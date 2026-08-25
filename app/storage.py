"""Recording storage: SQLite segment index + retention/quota purger.

Multi-root by design: RtcView can record across one or more storage
roots, each mounted by the admin outside the app (see README's Kurulum).
RtcView itself never formats, mounts, or unmounts a disk on ANY of these
paths — that boundary is deliberate. An earlier version of this feature
let RtcView manage disks itself (auto-mount/format), and a flaky
external-USB drive it was recording to went into a hardware failure
loop; the app's own automated per-disk purge logic reacted badly to that
instability and made a bad situation worse — a real production incident.
This reintroduces multiple write targets without reintroducing that: a
human mounts (and can unmount) every path here, RtcView only ever writes
to and deletes from paths it's told about.

Writes fill sequentially: the first configured root that currently has
room takes every new segment; once it doesn't, the next one takes over
(see pick_write_root()). One disk managed by a human per path, several
paths composed simply, is the goal — not automated disk topology
management.

Two separate, deliberately simple cleanup mechanisms, kept apart because
they react to different things:
  - purge_once() (background thread, every purge_interval_seconds):
    age-based retention and size-based quota — rules that don't need to
    react to a specific write event, just periodic sweeps.
  - free_up_for_new_segment() (called synchronously from recorder.py
    right before every new segment starts): the "a disk is full" case.
    A no-op unless EVERY configured root is below the configured
    low-space margin; when that happens, it deletes exactly N
    globally-oldest unlocked segments (N = configured camera count) and
    stops. Deliberately not "keep deleting until margin clears" — a
    fixed, predictable amount is simpler to reason about, and if it
    wasn't enough this call runs again at the very next segment start.
"""
import errno
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("storage")

# Fallback free-space floor (MB) when recording.low_space_margin_mb is
# missing/invalid — see Storage._margin_bytes(). The setting itself lives
# in config.py (recording.low_space_margin_mb) since 1 GB isn't the right
# number on every disk size; this is only the safe default.
DEFAULT_MARGIN_MB = 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cam_id       TEXT    NOT NULL,
    path         TEXT    NOT NULL UNIQUE,
    started_at   REAL    NOT NULL,
    ended_at     REAL    NOT NULL,
    duration     REAL    NOT NULL,
    bytes        INTEGER NOT NULL,
    locked       INTEGER NOT NULL DEFAULT 0,
    trigger      TEXT    NOT NULL DEFAULT 'schedule',
    playable     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_seg_cam_time ON segments(cam_id, started_at);
CREATE INDEX IF NOT EXISTS idx_seg_time     ON segments(started_at);

CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cam_id     TEXT    NOT NULL,
    path       TEXT    NOT NULL UNIQUE,
    taken_at   REAL    NOT NULL,
    bytes      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_cam_time ON snapshots(cam_id, taken_at);

CREATE TABLE IF NOT EXISTS detections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cam_id     TEXT    NOT NULL,
    kind       TEXT    NOT NULL,   -- 'motion' | 'person' | 'vehicle'
    started_at REAL    NOT NULL,
    ended_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_det_cam_time ON detections(cam_id, started_at);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cam_id     TEXT    NOT NULL,
    kind       TEXT    NOT NULL,   -- 'motion' | 'person' | 'vehicle'
    event_ts   REAL    NOT NULL,   -- wall-clock instant of the detection edge (for playback jump)
    created_at REAL    NOT NULL,   -- row insert time (for retention pruning)
    read       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications(created_at);
CREATE INDEX IF NOT EXISTS idx_notif_cam     ON notifications(cam_id, event_ts);
"""

# Notifications are a transient action log, not video — kept separate from
# recording.retention_days (which can be 0/unlimited and means something
# different: how long to keep footage).
NOTIF_RETENTION_DAYS = 30
NOTIF_MAX_ROWS = 2000

# How often the same root's write-test failure gets logged again — see
# Storage._log_root_error(). pick_write_root() alone can be called several
# times a second across multiple cameras, so logging every single failed
# probe on a persistently broken/unplugged disk would flood the log; this
# caps that while still surfacing the problem promptly the first time and
# periodically thereafter (not just once and never again).
ROOT_ERROR_LOG_COOLDOWN_SEC = 60


class Storage:
    """
    Thread-safe SQLite index for recording segments and snapshots plus a
    background purger enforcing retention (days) and quota (bytes) across
    one or more storage roots.

    index.sqlite itself lives in the app's config directory (next to
    config.json), NOT under any recording storage root. It used to live
    at a root's own index.sqlite, which meant changing/reordering
    storage_paths in Kayıt & Depolama settings — an ordinary thing to do
    when a disk fills up and another should take over — opened a
    brand-new empty database at the new location: every previously
    indexed segment vanished from the UI (playback, stats, health) even
    though every video file was untouched on disk. The index is app
    STATE, not recording data, and its location must not depend on which
    path(s) the admin currently points recording at.
    """

    def __init__(self, config_store):
        self.config_store = config_store
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        self._db_path: Optional[Path] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Cached derived state — a 4-s status poll shouldn't fire a real
        # write-test / statfs on every hit. Invalidated opportunistically
        # on register/delete/set_roots. _disk_cache is per-root (keyed by
        # resolved path string).
        self._disk_cache: dict[str, tuple[float, dict]] = {}
        self._stats_cache: Optional[tuple[float, dict]] = None
        self._health_cache: Optional[tuple[float, dict]] = None
        # Set when delete_segment() drops an index row but the file itself
        # fails to unlink (transient I/O hiccup, disk briefly unwritable) —
        # the file is now an untracked orphan still taking up space. The
        # purge loop rescans on its next tick to re-register it so it's
        # visible (and purgeable) again, instead of silently sitting there
        # forever until someone notices and clicks "Diski yeniden tara".
        self._orphan_suspected = False
        # Per-root cooldown for _log_root_error() — see its docstring.
        self._root_error_log_at: dict[str, float] = {}
        # "Diski yeniden tara" state, polled by the frontend instead of
        # blocking a single HTTP request for as long as the walk takes
        # (can be minutes on a large archive over slow SD-card/eMMC
        # storage) — see start_rescan_async()/get_rescan_status(). A
        # separate lock from self._lock: status polls must stay instant
        # even while a scan is mid-flight holding self._lock for a batch
        # insert.
        self._rescan_lock = threading.Lock()
        self._rescan_state: dict = {
            "running": False, "phase": "idle", "total": 0, "scanned": 0,
            "added": 0, "removed": 0, "done": True, "ok": True, "error": None,
            "started_at": None, "finished_at": None,
        }
        self._ensure_roots()

    # ---------- Roots / DB ----------
    def _rec_cfg(self):
        return self.config_store.get_recording()

    def roots(self) -> list[Path]:
        """All configured storage roots, in preference (fill) order.
        Blank entries dropped, duplicates (after resolving) collapsed to
        their first occurrence, ~ expanded. Falls back to the single
        built-in default if the config is somehow empty (shouldn't
        happen — config.py always seeds at least one)."""
        rc = self._rec_cfg()
        raw = rc.get("storage_paths") or []
        out: list[Path] = []
        seen: set = set()
        for r in raw:
            if isinstance(r, dict):
                r = r.get("path")
            s = str(r or "").strip()
            if not s:
                continue
            p = Path(s).expanduser().resolve()
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
        return out or [Path("/opt/rtcview/recordings").resolve()]

    def primary_root(self) -> Path:
        """The first configured root — where the index/snapshots live
        relative to, and the answer to any legacy single-root question."""
        return self.roots()[0]

    def root(self) -> Path:
        """Legacy single-root accessor, kept for the couple of call sites
        (recorder.py's usability pre-check, main.py's settings diffing)
        that only need "a" root, not the full list. Prefer roots()/
        pick_write_root()/has_usable_root() in new code."""
        return self.primary_root()

    def max_bytes(self) -> int:
        try:
            gb = int(self._rec_cfg().get("max_gb", 0) or 0)
        except (TypeError, ValueError):
            gb = 0
        return max(0, gb) * (1024**3)

    def _margin_bytes(self) -> int:
        """Free-space floor below which a root is "no room" — read fresh
        from config on every call (not cached: it's a rare, tiny read,
        and the write picker / purge loop must see a change immediately,
        not after some cache window). A non-positive or unparsable value
        falls back to DEFAULT_MARGIN_MB rather than disabling the floor —
        ffmpeg still needs some slack to flush a segment without hitting
        ENOSPC mid-write, so "no margin at all" is never a valid choice
        even if someone types 0 into the setting."""
        try:
            mb = int(self._rec_cfg().get("low_space_margin_mb", DEFAULT_MARGIN_MB) or DEFAULT_MARGIN_MB)
        except (TypeError, ValueError):
            mb = DEFAULT_MARGIN_MB
        if mb <= 0:
            mb = DEFAULT_MARGIN_MB
        return mb * 1024 * 1024

    def snapshot_roots(self) -> list[Path]:
        """The "_snapshots" subdir under every configured root — used to
        recognize/exclude snapshot files when walking for segments
        (rescan()) and to pre-create the folder on every root, not just
        the primary one."""
        return [r / "_snapshots" for r in self.roots()]

    def pick_snapshot_root(self) -> Path:
        """Sequential-fill destination for a NEW snapshot — same policy
        as pick_write_root() (segments), so a still shot doesn't insist
        on landing on a disk that's already full while segments have
        long since moved on to the next configured root. Snapshots used
        to always go under the primary (first-configured) root
        regardless of whether it had room, which meant they could start
        failing to save well before recording itself did."""
        return self.pick_write_root() / "_snapshots"

    @staticmethod
    def _like_escape(s: str) -> str:
        """Escape % / _ / \\ so a path can be used as a literal prefix in
        a SQL LIKE pattern (paired with ESCAPE '\\' at the call site). A
        root path containing either wildcard character — not exotic,
        e.g. /mnt/disk_1 or /media/usb_drive — would otherwise silently
        widen the match to unrelated paths that merely share the same
        prefix up to that character."""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _bytes_used(self) -> int:
        """SUM(bytes) for every segment in the index, across every root.
        Quota is global (not per-disk — see the module docstring), so
        this needs no path scoping."""
        with self._lock:
            row = self._db.execute("SELECT COALESCE(SUM(bytes), 0) FROM segments").fetchone()
        return int(row[0] or 0)

    def _bytes_used_under(self, root_path: str) -> int:
        """SUM(bytes) for segments whose path is under root_path
        specifically — used by the per-root reclaim loop (each root's own
        rolling buffer is judged against its own footage, not the global
        total) via an indexed LIKE 'prefix%' range scan rather than
        pulling every segment into Python."""
        pattern = self._like_escape(root_path + os.sep) + "%"
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM segments WHERE path LIKE ? ESCAPE '\\'",
                (pattern,)
            ).fetchone()
        return int(row[0] or 0)

    def _has_purgeable_segments(self, root_path: Optional[str] = None) -> bool:
        """Whether an unlocked segment exists — globally when root_path
        is None (used by the emergency purge, which deletes the
        globally-oldest segment regardless of which root it's on), or
        scoped to segments actually living under root_path (used by
        health()'s per-root check).

        The scoped form matters: a root that's itself out of purgeable
        segments is a dead end for THAT root specifically even if some
        other, unrelated root still has plenty to reclaim from — deleting
        root B's old footage frees no space and fixes nothing on root A.
        Checking globally there would silently report a genuinely broken
        or exhausted root A as "ok" purely because root B happens to have
        something purgeable, masking a real per-root problem (see
        health())."""
        with self._lock:
            if root_path is None:
                row = self._db.execute("SELECT 1 FROM segments WHERE locked = 0 LIMIT 1").fetchone()
            else:
                pattern = self._like_escape(root_path + os.sep) + "%"
                row = self._db.execute(
                    "SELECT 1 FROM segments WHERE locked = 0 AND path LIKE ? ESCAPE '\\' LIMIT 1",
                    (pattern,)
                ).fetchone()
        return row is not None

    def _log_root_error(self, root: Path, err: Exception):
        """Log that `root` failed its write-test/usability probe,
        rate-limited per root (see ROOT_ERROR_LOG_COOLDOWN_SEC) so a
        persistently broken/unplugged/corrupted disk doesn't flood the
        log — pick_write_root() alone can be called several times a
        second across multiple cameras. The sequential-fill model already
        tries the next configured root automatically on any failure here
        (see pick_write_root()); this is purely so the admin can SEE
        which specific root is the problem instead of only noticing
        recording quietly favoring a different disk."""
        key = str(root)
        now = time.time()
        if now - self._root_error_log_at.get(key, 0.0) < ROOT_ERROR_LOG_COOLDOWN_SEC:
            return
        self._root_error_log_at[key] = now
        log.error("storage root unusable, trying the next configured root: %s (%s)", root, err)

    def pick_write_root(self) -> Path:
        """Sequential fill: try each configured root in order, return the
        FIRST one that's writable and has at least the configured low-
        space margin (see _margin_bytes()) free right now. This is the
        "önce A dolsun, sonra B" model — as
        long as A has room, every new segment goes to A; once A can't fit
        another safety-buffered write, B takes over automatically.

        If every root is out of room, return whichever root was merely
        writable (last one checked that passed the write-test) so the
        current segment still lands somewhere rather than the caller
        getting None — purge_once()'s emergency path reclaims space on
        its next tick. If NO root is even writable, return the primary
        anyway (matches the single-root behavior this always had:
        ffmpeg's own write failure is what actually surfaces the
        problem, not a pre-emptive None here)."""
        margin = self._margin_bytes()
        fallback: Optional[Path] = None
        for r in self.roots():
            try:
                r.mkdir(parents=True, exist_ok=True)
                probe = r / f".rtcview_write_test.{os.getpid()}.{threading.get_ident()}"
                probe.write_text("ok"); probe.unlink()
                du = shutil.disk_usage(str(r))
            except Exception as e:
                self._log_root_error(r, e)
                continue
            fallback = r
            if du.free < margin:
                continue
            return r
        return fallback or self.primary_root()

    def has_usable_root(self) -> bool:
        """True if AT LEAST ONE configured root is currently writable —
        used where a single-root usability gate used to check root()
        alone (e.g. recorder.py deciding whether to even attempt
        building a recorder). A specific root being unusable is fine as
        long as another one can still take the write; only "every root
        is broken" should actually block recording."""
        for r in self.roots():
            try:
                r.mkdir(parents=True, exist_ok=True)
                probe = r / f".rtcview_write_test.{os.getpid()}.{threading.get_ident()}"
                probe.write_text("ok"); probe.unlink()
                return True
            except Exception as e:
                self._log_root_error(r, e)
                continue
        return False

    def _any_root_has_room(self, margin: int) -> bool:
        """True if AT LEAST ONE configured root is both writable and has
        at least `margin` bytes free right now — the same "room" that
        pick_write_root() looks for. A root with plenty of free bytes but
        a broken write (wrong permissions after a remount, for example)
        is NOT room."""
        for r in self.roots():
            try:
                probe = r / f".rtcview_write_test.{os.getpid()}.{threading.get_ident()}"
                probe.write_text("ok"); probe.unlink()
                du = shutil.disk_usage(str(r))
            except Exception as e:
                self._log_root_error(r, e)
                continue
            if du.free >= margin:
                return True
        return False

    def free_up_for_new_segment(self) -> dict:
        """Called right before every new segment or snapshot starts
        (recorder.py's CameraRecorder.start()/_finish_segment(), main.py's
        snapshot route) — the simple replacement for the old periodic
        "reclaim" machinery. Also called once per purge_once() tick as a
        backstop, so a disk that fills up from something OTHER than
        active recording (recording paused/disabled, manual file copies,
        snapshot volume) still gets cleaned up periodically instead of
        only reacting to segment events that might not be happening.

        If ANY configured root already has room, this does nothing: the
        sequential-fill write picker (pick_write_root()) will just use
        that root, no need to delete anything. Deleting is only ever
        useful once EVERY root is below the configured margin — that's
        the one moment recording genuinely has nowhere to go without
        making room first.

        When that happens, it deletes up to N globally-oldest unlocked
        segments, where N is the number of cameras currently configured
        to record (record_mode != "off" — a disabled camera contributes
        no write pressure, so it shouldn't inflate how much gets deleted
        on its behalf). This is a fixed, predictable amount per call, not
        a "keep deleting until X" loop: simple and easy to reason about,
        at the cost of not being exactly precise about how many bytes it
        actually frees (that's fine — if it wasn't enough, the very next
        segment start calls this again).

        A delete whose unlink fails doesn't abort the whole pass anymore
        — that segment's root is skipped for the REST OF THIS CALL ONLY
        (not retried, not hammered), and the loop keeps going through
        further candidates so a different, healthy root still gets its
        segments cleaned up. Without this, a single permanently-broken
        root sitting at the front of the oldest-first ordering could
        block cleanup on every OTHER root forever, since its segments
        would always sort first — a milder reappearance of the exact
        cross-root cascade this module's whole design exists to avoid.
Once a root has refused a delete, it's excluded from the SQL query
        itself (a NOT LIKE clause) for the rest of this call, not merely
        skipped while iterating a fixed batch — a broken root can hold
        arbitrarily many segments older than anything on a healthy root
        (that's the normal, expected shape in a sequential-fill setup:
        whichever root filled up first has the oldest footage), so a
        fixed-size batch pulled once up front can end up entirely
        consumed by a broken root's (skipped) rows before a healthy
        root's candidates ever appear in it. Querying one row at a time
        and excluding known-broken roots as they're discovered can never
        get stuck that way, however lopsided the age distribution is.
        The file that failed to unlink is still an orphan on disk — the
        next purge_once() tick's auto-rescan recovers it
        (self._orphan_suspected).

        The whole check-then-delete sequence runs under self._lock (an
        RLock, so nested calls into _delete_segment_impl()'s own locking
        are fine) — every camera's segment rollover can call this at
        roughly the same moment (several cameras sharing the same
        segment_seconds tend to roll over in the same few seconds), plus
        purge_once()'s own backstop call, plus the snapshot route. Without
        serializing the full "is there room? no -> delete N" decision,
        several concurrent callers could each independently observe "no
        room" and each go on to delete their own N segments, deleting
        N*(number of concurrent callers) instead of N — real, avoidable
        data loss, not just a cosmetic race. A caller that has to wait
        for the lock re-checks room fresh once it gets it, so if an
        earlier caller already fixed things, the rest are cheap no-ops."""
        with self._lock:
            margin = self._margin_bytes()
            if self._any_root_has_room(margin):
                return {"removed": 0, "freed_bytes": 0}
            n = max(1, sum(1 for c in self.config_store.get_cameras()
                           if c.get("record_mode", "off") != "off"))
            log.warning("free_up_for_new_segment: no root has room — deleting up to %d globally-oldest segment(s)", n)
            removed = 0; freed = 0
            broken_roots: set = set()
            attempted_any = False
            while removed < n:
                if broken_roots:
                    where = " AND ".join(["path NOT LIKE ? ESCAPE '\\'"] * len(broken_roots))
                    patterns = [self._like_escape(str(r) + os.sep) + "%" for r in broken_roots]
                    row = self._db.execute(
                        f"SELECT id, bytes, path FROM segments WHERE locked = 0 AND ({where})"
                        f" ORDER BY started_at ASC LIMIT 1",
                        (*patterns,)
                    ).fetchone()
                else:
                    row = self._db.execute(
                        "SELECT id, bytes, path FROM segments WHERE locked = 0"
                        " ORDER BY started_at ASC LIMIT 1"
                    ).fetchone()
                if not row:
                    break  # nothing left anywhere (outside already-broken roots)
                attempted_any = True
                sid, b, path = row
                deleted, unlink_ok = self._delete_segment_impl(sid)
                if not deleted:
                    continue  # already gone/locked — not a failure, try the next one
                if not unlink_ok:
                    # The row is already gone from the index regardless
                    # (_delete_segment_impl drops it before attempting the
                    # unlink), so the next query naturally won't return
                    # this same row again even if its root can't be
                    # identified (an orphaned reference to a since-removed
                    # storage path, say) -- nothing to exclude in that
                    # case, just move on.
                    seg_root = self._root_for_path(path)
                    if seg_root is not None:
                        broken_roots.add(seg_root)
                        log.warning("free_up_for_new_segment: %s — a delete failed, excluding it "
                                    "for the rest of this pass (other roots continue)", seg_root)
                    continue
                removed += 1
                freed += int(b or 0)
            if not attempted_any:
                log.error("free_up_for_new_segment: no room anywhere and nothing left to delete "
                           "(everything locked?) — the new segment may fail to write")
            return {"removed": removed, "freed_bytes": freed}

    def _ensure_roots(self):
        try:
            for r in self.roots():
                r.mkdir(parents=True, exist_ok=True)
            for sr in self.snapshot_roots():
                sr.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("Cannot create storage root: %s", e)
            return
        # Stable regardless of storage_paths order/content — see the class
        # docstring. config_path's parent is guaranteed to exist already
        # (ConfigStore creates it), mkdir here is just defensive.
        index_dir = self.config_store.config_path.parent
        index_dir.mkdir(parents=True, exist_ok=True)
        db_path = index_dir / "index.sqlite"
        with self._lock:
            if self._db_path == db_path and self._db is not None:
                return
            if self._db is not None:
                try: self._db.close()
                except Exception: pass
            is_fresh = not db_path.exists()
            if is_fresh:
                # One-time upgrade path: installs from before the index
                # moved out of the recording roots have their real history
                # sitting at <some root>/index.sqlite. Move it (with its
                # WAL/SHM sidecars) to the new stable location instead of
                # starting over. Best-effort — a failure here just means a
                # fresh (empty) index opens below, same as any other
                # first-run; it must never block startup.
                self._migrate_legacy_index(db_path)
                is_fresh = not db_path.exists()
            self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
            if is_fresh:
                # Must be set before the schema below creates any table —
                # auto_vacuum only takes effect on a database that doesn't
                # have one yet (changing it later needs a full VACUUM,
                # which would block startup on an existing, possibly large,
                # file for no clear win — so existing databases just keep
                # whatever mode they already have).
                self._db.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self._db.executescript(SCHEMA)
            # One-time column migration for DBs created before the
            # 'playable' column existed — executescript's CREATE TABLE IF
            # NOT EXISTS above is a no-op on an already-existing segments
            # table, so older indexes never pick up new columns on their
            # own. Existing rows default to 1 (playable): we have no way
            # to retroactively verify their trailers cheaply, and treating
            # unknown history as fine avoids mass-flagging a working index.
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(segments)").fetchall()}
            if "playable" not in cols:
                self._db.execute("ALTER TABLE segments ADD COLUMN playable INTEGER NOT NULL DEFAULT 1")
            # PRAGMA tuning — WAL for concurrency, mmap for cheap reads,
            # 4 MB page cache (default 2 MB), temp tables in RAM.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA cache_size=-4000")
            self._db.execute("PRAGMA mmap_size=67108864")
            self._db.execute("PRAGMA temp_store=MEMORY")
            self._db_path = db_path
            log.info("Storage index open: %s", db_path)

    def _migrate_legacy_index(self, new_path: Path):
        for root in self.roots():
            old_path = root / "index.sqlite"
            if old_path == new_path or not old_path.exists():
                continue
            try:
                for suffix in ("", "-wal", "-shm"):
                    src = Path(str(old_path) + suffix)
                    if src.exists():
                        shutil.move(str(src), str(new_path) + suffix)
                log.info("Migrated recording index from %s to %s", old_path, new_path)
            except Exception as e:
                log.warning("Could not migrate legacy index from %s: %s", old_path, e)
            return  # only one legacy location is ever plausible; stop either way

    def set_roots(self, new_paths) -> tuple[bool, str]:
        """Replace the entire storage_paths list. Every path must be
        creatable and writable (validated with a unique-per-call probe
        filename — see pick_write_root()'s comment on why a shared fixed
        name would race under concurrent callers); the whole list is
        rejected if any single path fails, so a typo can't silently drop
        a working disk out of rotation."""
        if not new_paths:
            return False, "En az bir kayıt klasörü olmalı"
        clean: list[str] = []
        seen: set = set()
        for np in new_paths:
            if isinstance(np, dict):
                np = np.get("path")
            s = str(np or "").strip()
            if not s:
                continue
            p = Path(s).expanduser()
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return False, f"Klasör oluşturulamadı: {p} ({e})"
            probe = p / f".rtcview_write_test.{os.getpid()}.{threading.get_ident()}"
            try:
                probe.write_text("ok"); probe.unlink()
            except Exception as e:
                return False, f"Klasöre yazılamıyor: {p} ({e})"
            rp = str(p.resolve())
            if rp in seen:
                continue
            seen.add(rp)
            clean.append(rp)
        if not clean:
            return False, "Geçerli klasör bulunamadı"
        self.config_store.update_recording({"storage_paths": clean})
        self._ensure_roots()
        self._invalidate_caches()
        return True, ", ".join(clean)

    def _invalidate_caches(self):
        self._stats_cache = None
        self._health_cache = None
        self._disk_cache.clear()

    # ---------- Segments ----------
    def register_segment(self, cam_id: str, path: str, started_at: float,
                         ended_at: float, trigger: str = "schedule",
                         playable: Optional[bool] = None) -> Optional[int]:
        try:
            size = os.path.getsize(path)
        except OSError:
            log.warning("register_segment: file gone before index: %s", path)
            return None
        duration = max(0.0, ended_at - started_at)
        abs_path = str(Path(path).resolve())
        # None (caller couldn't check, e.g. non-mp4 container) defaults
        # to playable — we only ever flag on a positive, verified failure.
        playable_val = 0 if playable is False else 1
        # A new segment changes bytes_used (quota/health decisions consume
        # it) and disk usage (the file just landed). Invalidate every
        # cache, not just stats — a stale health cache would leave the
        # emergency-purge check working from old numbers.
        self._invalidate_caches()
        with self._lock:
            # INSERT OR IGNORE preserves the row (and its id + locked flag) if
            # a segment is re-registered; then an UPDATE tops up the ended_at
            # / bytes / trigger values. INSERT OR REPLACE would delete-then-
            # insert with a new id, breaking any URLs the client already holds.
            cur = self._db.execute(
                "INSERT OR IGNORE INTO segments"
                " (cam_id, path, started_at, ended_at, duration, bytes, trigger, playable)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (cam_id, abs_path, started_at, ended_at, duration, size, trigger, playable_val),
            )
            if cur.rowcount == 0:
                self._db.execute(
                    "UPDATE segments SET ended_at=?, duration=?, bytes=?, trigger=?, playable=?"
                    " WHERE path=?",
                    (ended_at, duration, size, trigger, playable_val, abs_path),
                )
                r = self._db.execute("SELECT id FROM segments WHERE path=?", (abs_path,)).fetchone()
                return r[0] if r else None
            return cur.lastrowid

    def list_segments(self, cam_id: Optional[str] = None,
                      t_from: Optional[float] = None,
                      t_to: Optional[float] = None,
                      limit: int = 2000) -> list[dict]:
        q = "SELECT id, cam_id, path, started_at, ended_at, duration, bytes, locked, trigger, playable FROM segments"
        conds, args = [], []
        if cam_id:
            conds.append("cam_id = ?"); args.append(cam_id)
        if t_from is not None:
            conds.append("ended_at >= ?"); args.append(t_from)
        if t_to is not None:
            conds.append("started_at <= ?"); args.append(t_to)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY started_at ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        cols = ["id","cam_id","path","started_at","ended_at","duration","bytes","locked","trigger","playable"]
        return [dict(zip(cols, r)) for r in rows]

    def get_segment(self, seg_id: int) -> Optional[dict]:
        with self._lock:
            r = self._db.execute(
                "SELECT id, cam_id, path, started_at, ended_at, duration, bytes, locked, trigger, playable"
                " FROM segments WHERE id = ?", (seg_id,)
            ).fetchone()
        if not r: return None
        cols = ["id","cam_id","path","started_at","ended_at","duration","bytes","locked","trigger","playable"]
        return dict(zip(cols, r))

    def get_segment_by_path(self, path: str) -> Optional[dict]:
        """Same as get_segment, keyed by the file's own path instead of
        its row id — for callers (recorder.py's _scan_and_register) that
        only know the path, not whatever id a previous register_segment()
        call returned."""
        abs_path = str(Path(path).resolve())
        with self._lock:
            r = self._db.execute(
                "SELECT id, cam_id, path, started_at, ended_at, duration, bytes, locked, trigger, playable"
                " FROM segments WHERE path = ?", (abs_path,)
            ).fetchone()
        if not r: return None
        cols = ["id","cam_id","path","started_at","ended_at","duration","bytes","locked","trigger","playable"]
        return dict(zip(cols, r))

    def _root_for_path(self, path: str) -> Optional[Path]:
        """Which configured root a given absolute segment path actually
        lives under, if any — used to prune the correct empty date/cam
        subdirs after a delete. A segment from a root that's since been
        removed from storage_paths won't match anything; that's fine,
        pruning is just tidiness, not correctness."""
        try:
            p = Path(path).resolve()
        except Exception:
            return None
        for r in self.roots():
            try:
                if r in p.parents:
                    return r
            except Exception:
                continue
        return None

    def delete_segment(self, seg_id: int, force: bool = False) -> bool:
        return self._delete_segment_impl(seg_id, force)[0]

    def _delete_segment_impl(self, seg_id: int, force: bool = False) -> tuple[bool, bool]:
        """Does the actual work behind delete_segment(); returns (deleted,
        unlink_ok) instead of just deleted. The purge loop's
        _delete_and_keep_going needs to know whether THIS SPECIFIC call's
        unlink succeeded — self._orphan_suspected is instance-wide and
        sticky for the rest of the current purge_once() tick (by design,
        so the NEXT tick's auto-rescan can recover from it — see its own
        docstring), so checking that shared flag here would make every
        delete AFTER the first failure in a tick look like a failure too,
        even a perfectly successful one on a completely different, healthy
        root. That defeats the whole point of the per-root isolation the
        purge loop relies on: one root's trouble must never make a
        different root's own successful deletes get treated as failures."""
        seg = self.get_segment(seg_id)
        if not seg: return False, True
        # The locked check and the delete are one atomic statement (both
        # under self._lock) rather than "read locked, decide, then
        # delete" — the latter left a window where a concurrent
        # set_locked(seg_id, True) (a user protecting a segment right as
        # a purge was about to remove it) could land in between the
        # check and the delete and still lose the file, since the delete
        # would proceed on the already-stale "unlocked" read.
        with self._lock:
            if force:
                cur = self._db.execute("DELETE FROM segments WHERE id = ?", (seg_id,))
            else:
                cur = self._db.execute("DELETE FROM segments WHERE id = ? AND locked = 0", (seg_id,))
            deleted = cur.rowcount > 0
        if not deleted:
            return False, True
        # Unlink AFTER the row is confirmed gone (and confirmed not
        # locked, atomically, per above). If the OS refuses the unlink
        # (permission, ENOENT, a disk so full even deleting fails), the
        # index row is already dropped — self._orphan_suspected flags it
        # so the next purge tick rescans and re-registers the file (still
        # physically on disk) so it isn't silently forgotten.
        unlink_ok = True
        try:
            Path(seg["path"]).unlink(missing_ok=True)
        except Exception as e:
            log.warning("delete_segment: unlink failed %s: %s", seg["path"], e)
            self._orphan_suspected = True
            unlink_ok = False
        # Invalidate everything: the reclaim/write-picker logic relies on
        # fresh bytes_used AND disk_usage numbers.
        self._invalidate_caches()
        # Prune empty date/cam subdirs up to (not including) whichever
        # root this segment actually belonged to.
        seg_root = self._root_for_path(seg["path"])
        if seg_root is not None:
            parent = Path(seg["path"]).parent
            try:
                _try_prune_empty(parent, seg_root)
            except Exception:
                pass
        return True, unlink_ok

    def set_locked(self, seg_id: int, locked: bool) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE segments SET locked=? WHERE id=?",
                                   (1 if locked else 0, seg_id))
            return cur.rowcount > 0

    # ---------- Snapshots ----------
    def register_snapshot(self, cam_id: str, path: str, taken_at: float) -> Optional[int]:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        with self._lock:
            cur = self._db.execute(
                "INSERT OR REPLACE INTO snapshots (cam_id, path, taken_at, bytes) VALUES (?,?,?,?)",
                (cam_id, str(Path(path).resolve()), taken_at, size),
            )
            return cur.lastrowid

    def list_snapshots(self, cam_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        q = "SELECT id, cam_id, path, taken_at, bytes FROM snapshots"
        args: list = []
        if cam_id:
            q += " WHERE cam_id = ?"; args.append(cam_id)
        q += " ORDER BY taken_at DESC LIMIT ?"; args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        cols = ["id","cam_id","path","taken_at","bytes"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------- Detections (ONVIF motion / person intervals) ----------
    def open_detection(self, cam_id: str, kind: str, ts: float) -> Optional[int]:
        """Start a new detection interval; returns its row id (or None on
        failure — callers must tolerate a missing id, it just means this
        one edge won't be reflected on the timeline)."""
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO detections (cam_id, kind, started_at, ended_at) VALUES (?,?,?,?)",
                    (cam_id, kind, ts, ts),
                )
                return cur.lastrowid
        except Exception as e:
            log.debug("open_detection failed: %s", e)
            return None

    def extend_detection(self, det_id: int, ts: float):
        """Push an open interval's end forward — called on repeat 'still
        active' events and on final close (explicit stop or timeout)."""
        if det_id is None:
            return
        try:
            with self._lock:
                self._db.execute("UPDATE detections SET ended_at=? WHERE id=?", (ts, det_id))
        except Exception as e:
            log.debug("extend_detection failed: %s", e)

    def list_detections(self, cam_id: Optional[str] = None,
                        t_from: Optional[float] = None,
                        t_to: Optional[float] = None,
                        limit: int = 5000) -> list[dict]:
        q = "SELECT id, cam_id, kind, started_at, ended_at FROM detections"
        conds, args = [], []
        if cam_id:
            conds.append("cam_id = ?"); args.append(cam_id)
        if t_from is not None:
            conds.append("ended_at >= ?"); args.append(t_from)
        if t_to is not None:
            conds.append("started_at <= ?"); args.append(t_to)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY started_at ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        cols = ["id", "cam_id", "kind", "started_at", "ended_at"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------- Notifications ----------
    def create_notification(self, cam_id: str, kind: str, event_ts: float) -> Optional[int]:
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO notifications (cam_id, kind, event_ts, created_at) VALUES (?,?,?,?)",
                    (cam_id, kind, event_ts, time.time()),
                )
                return cur.lastrowid
        except Exception as e:
            log.debug("create_notification failed: %s", e)
            return None

    def list_notifications(self, unread_only: bool = False, limit: int = 200) -> list[dict]:
        q = "SELECT id, cam_id, kind, event_ts, created_at, read FROM notifications"
        if unread_only:
            q += " WHERE read = 0"
        q += " ORDER BY event_ts DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(q, (limit,)).fetchall()
        cols = ["id", "cam_id", "kind", "event_ts", "created_at", "read"]
        return [dict(zip(cols, r)) for r in rows]

    def mark_all_notifications_read(self):
        with self._lock:
            self._db.execute("UPDATE notifications SET read = 1 WHERE read = 0")

    def clear_all_notifications(self):
        with self._lock:
            self._db.execute("DELETE FROM notifications")

    # ---------- Health ----------
    def health(self) -> dict:
        """Report the storage subsystem's live state across ALL roots.

        Cached 15 s so a 4-s polling client doesn't fire a real
        write-test / statfs on every hit. Cache is invalidated by
        set_roots() and by segment register/delete.

        Overall status = worst-of-any-root: "ok" | "warning" | "error"
        """
        now = time.time()
        cached = self._health_cache
        if cached and (now - cached[0]) < 15:
            return cached[1]

        errors: list[str] = []
        warnings: list[str] = []
        per_root: list[dict] = []
        overall = "ok"
        margin = self._margin_bytes()
        for r in self.roots():
            r_errs: list[str] = []; r_warns: list[str] = []
            exists = r.exists()
            writable = False
            disk = {"total": 0, "free": 0, "used": 0}
            free_pct = 100.0
            if not exists:
                r_errs.append(f"Klasör yok: {r}")
            else:
                # Disk usage first — the write-test error below needs to
                # know whether free space is already critically low, since
                # some filesystems (f2fs in particular, once it runs out of
                # free segments for a checkpoint) return EIO instead of
                # ENOSPC for what is still fundamentally a "disk is full"
                # condition, not a permissions/hardware fault.
                du = None
                try:
                    du = shutil.disk_usage(str(r))
                    disk = {"total": du.total, "free": du.free, "used": du.used}
                    free_pct = (du.free / du.total) * 100 if du.total else 0
                except Exception as e:
                    r_errs.append(f"Disk okunamadı: {e}")
                near_full = du is not None and du.free < margin
                # Unique per call (pid+thread), not a fixed name: health()
                # is cached for 15s but that cache check-then-set isn't
                # atomic, so a burst of concurrent callers (several
                # browser polls queued while a mobile tab was backgrounded
                # all firing at once on resume) can each decide to run a
                # fresh write-test on the same root at the same moment.
                # With a shared fixed filename, one caller's unlink()
                # removing the file before another caller's own unlink()
                # runs raised a genuine FileNotFoundError reported as
                # "Yazılamıyor: ..." even though nothing was actually
                # wrong.
                test = r / f".rtcview_write_test.{os.getpid()}.{threading.get_ident()}"
                write_err: Optional[Exception] = None
                try:
                    test.write_text("ok"); test.unlink()
                    writable = True
                except Exception as e:
                    write_err = e
                if write_err is not None:
                    is_space_related = near_full or (
                        isinstance(write_err, OSError) and write_err.errno == errno.ENOSPC
                    )
                    if is_space_related:
                        # A root this full — whether the write-test hit
                        # ENOSPC directly or disk_usage() already shows it
                        # under the 1 GB margin — is the SAME "disk
                        # kapasitesine yakın" story: the rolling/retention
                        # purge doesn't care how it got full, only that
                        # something old enough on THIS root still exists to
                        # reclaim. A write failure on a root that ISN'T
                        # actually low on space is NOT something purging
                        # can ever fix (wrong permissions, read-only fs,
                        # hardware fault, unmounted), so that stays a hard
                        # error, unconditionally.
                        #
                        # Scoped to THIS root (str(r)), not checked
                        # globally: a broken/corrupted root that happens
                        # to report near-full free space would otherwise
                        # get silently masked as "ok" purely because some
                        # OTHER, unrelated root still has old footage to
                        # purge — deleting root B's segments frees no
                        # space and fixes nothing on a genuinely broken
                        # root A.
                        if not self._has_purgeable_segments(str(r)):
                            r_errs.append(
                                "Yazılamıyor: disk dolu — silinecek kayıt kalmadı, kayıt durabilir"
                            )
                    else:
                        r_errs.append(f"Yazılamıyor: {write_err}")
                elif near_full and not self._has_purgeable_segments(str(r)):
                    # A root sitting right at capacity is the EXPECTED
                    # steady state for an unlimited-quota (max_gb=0)
                    # setup once footage volume outgrows it — purge_once()
                    # keeps deleting THIS root's oldest segments to hold
                    # it just above the configured low-space margin,
                    # forever, by design. Not worth flagging while that's still
                    # working — only a genuine dead end for THIS root
                    # (nothing of its own left to purge — see the scoping
                    # note above) is actionable.
                    r_errs.append(
                        f"Disk doldu: {du.free // (1024*1024)} MB boş — "
                        "silinecek kayıt kalmadı, kayıt durabilir"
                    )
            rst = "error" if r_errs else ("warning" if r_warns else "ok")
            per_root.append({
                "path": str(r), "status": rst, "exists": exists, "writable": writable,
                "disk": disk, "free_percent": round(free_pct, 1),
                "bytes_used": self._bytes_used_under(str(r)),
                "errors": r_errs, "warnings": r_warns,
            })
            for e in r_errs: errors.append(f"{r}: {e}")
            for w in r_warns: warnings.append(f"{r}: {w}")
            if rst == "error" or (rst == "warning" and overall == "ok"):
                overall = rst

        used_by_rec = self._bytes_used()
        max_bytes = self.max_bytes()
        if max_bytes > 0:
            q_pct = round((used_by_rec / max_bytes) * 100, 1)
            if q_pct >= 100:
                warnings.append(f"Kota dolu ({max_bytes // (1024**3)} GB)")
            elif q_pct >= 90:
                warnings.append(f"Kota %{q_pct:.0f}")

        # DB itself
        db_ok = False
        try:
            with self._lock:
                self._db.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception as e:
            errors.append(f"DB erişim: {e}")

        # Aggregate disk = SUM across every configured root, so a
        # multi-disk setup reports combined free/total (matches stats()).
        agg = {"total": 0, "free": 0, "used": 0}
        for pr in per_root:
            for k in ("total", "free", "used"):
                agg[k] += pr["disk"].get(k, 0)
        agg_free_pct = round((agg["free"] / agg["total"]) * 100, 1) if agg["total"] else 100.0

        primary = str(self.primary_root())
        prim = next((x for x in per_root if x["path"] == primary), None)
        overall_status = "error" if errors else ("warning" if warnings else "ok")
        result = {
            "status": overall_status,
            "root": primary,                      # legacy scalar for UI compat
            "roots": per_root,                     # per-root breakdown
            "exists": bool(prim and prim["exists"]),
            # ANY root being writable, not just the primary (first-listed)
            # one — the whole point of the sequential-fill model is that
            # the primary root is EXPECTED to eventually fill up and go
            # unwritable while recording keeps working fine via whichever
            # other root still has room. Reporting this as "SALT-OKUR"
            # whenever specifically the primary root goes full would read
            # as "recording is broken" when it very much isn't.
            "writable": any(pr["writable"] for pr in per_root),
            "db_ok": db_ok,
            "disk": agg,
            "free_percent": agg_free_pct,
            "bytes_used": used_by_rec,
            "max_gb": max_bytes // (1024**3) if max_bytes else 0,
            "max_bytes": max_bytes,
            "errors": errors,
            "warnings": warnings,
        }
        self._health_cache = (now, result)
        return result

    # ---------- Stats ----------
    def stats(self) -> dict:
        now_ts = time.time()
        cached = self._stats_cache
        if cached and (now_ts - cached[0]) < 5:
            return cached[1]

        with self._lock:
            total_bytes, total_count, oldest_started_at = self._db.execute(
                "SELECT COALESCE(SUM(bytes),0), COUNT(*), MIN(started_at) FROM segments"
            ).fetchone()
            per_cam = self._db.execute(
                "SELECT cam_id, COALESCE(SUM(bytes),0), COUNT(*),"
                " MIN(started_at), MAX(ended_at)"
                " FROM segments GROUP BY cam_id"
            ).fetchall()

        roots_stats = []
        agg = {"total": 0, "free": 0, "used": 0}
        for r in self.roots():
            key = str(r)
            dc = self._disk_cache.get(key)
            if dc and (now_ts - dc[0]) < 5:
                d = dc[1]
            else:
                try:
                    du = shutil.disk_usage(key)
                    d = {"total": du.total, "free": du.free, "used": du.used}
                except Exception:
                    d = {"total": 0, "free": 0, "used": 0}
                self._disk_cache[key] = (now_ts, d)
            roots_stats.append({
                "path": key,
                "disk": d,
                "bytes_used": self._bytes_used_under(key),
            })
            for k in ("total", "free", "used"): agg[k] += d[k]

        rc = self._rec_cfg()
        max_bytes = self.max_bytes()
        result = {
            "root": str(self.primary_root()),   # legacy scalar
            "roots": roots_stats,                # per-root disk+usage breakdown
            "bytes_used": int(total_bytes),
            "segment_count": int(total_count),
            # None when there are no segments at all yet, otherwise the
            # started_at of the single oldest segment across every camera
            # and root — lets the Kayıt & Depolama tab show exactly how
            # far back current footage goes, which is also a direct,
            # concrete answer to "when will retention start freeing up
            # space" (once this timestamp crosses retention_days).
            "oldest_started_at": float(oldest_started_at) if oldest_started_at is not None else None,
            "max_gb": max_bytes // (1024**3) if max_bytes else 0,
            "max_bytes": max_bytes,
            "retention_days": int(rc.get("retention_days", 14)),
            "disk": agg,                         # aggregate across every root
            "per_camera": [
                {"cam_id": c, "bytes": int(b), "count": int(n),
                 "first_at": s, "last_at": e}
                for (c, b, n, s, e) in per_cam
            ],
        }
        self._stats_cache = (now_ts, result)
        return result

    # ---------- Purger ----------
    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._purge_loop, daemon=True, name="rtcview-purger")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=3)
        with self._lock:
            if self._db:
                try: self._db.close()
                except Exception: pass
                self._db = None

    def _purge_loop(self):
        while not self._stop.is_set():
            try:
                if self._rec_cfg().get("enabled", True):
                    self.purge_once()
            except Exception as e:
                log.warning("purge failed: %s", e)
            self._stop.wait(int(self._rec_cfg().get("purge_interval_seconds", 60)))

    def purge_once(self) -> dict:
        if self._orphan_suspected:
            # A previous delete_segment() dropped a row but couldn't
            # unlink the file — re-register whatever's still sitting on
            # disk untracked before this tick's purge logic runs, so it's
            # visible to retention/quota/reclaim below instead of
            # invisibly holding onto space forever.
            self._orphan_suspected = False
            try:
                self.rescan()
            except Exception as e:
                log.warning("purge_once: auto-rescan for suspected orphan failed: %s", e)
        rc = self._rec_cfg()
        removed = 0; freed = 0
        retention = int(rc.get("retention_days", 14))
        now_ts = time.time()

        def _delete_and_keep_going(sid, b) -> tuple[bool, bool]:
            """Delete one segment. Returns (freed_ok, keep_going).

            keep_going turns False the instant a delete drops the index
            row without actually freeing the file (e.g. a filesystem so
            completely full that even unlink()'s own metadata write
            fails, or the disk went away mid-operation — observed in
            production on f2fs, and the original incident that shaped
            this whole safety pattern): every purge phase below must stop
            right there instead of ploughing through its whole remaining
            batch/table, since continuing only turns more of that root's
            tracked history into untracked orphans for zero space gained
            — and, critically, must not let one root's trouble affect any
            OTHER root's phase (each call site below scopes its own
            batch/rows to a single root or, for retention/quota, treats
            a stop here as "pause this tick", never "keep trying this
            root harder"). The next tick's auto-rescan (top of this
            function) recovers whatever got orphaned this way.

            freed_ok is True only when the file was genuinely deleted, so
            callers doing their own byte accounting (the quota loop's
            running `over`) don't have to re-derive success.

            Checks THIS call's own unlink result (_delete_segment_impl's
            second return value), not self._orphan_suspected — that flag
            is deliberately sticky for the rest of the tick (see its own
            docstring), so reading it here would make every delete AFTER
            the first failure anywhere look like a failure too, including
            a perfectly successful one on a different, healthy root."""
            nonlocal removed, freed
            deleted, unlink_ok = self._delete_segment_impl(sid)
            if not deleted:
                return False, True   # already gone/locked — not a failure, keep going
            if not unlink_ok:
                return False, False  # row dropped, file NOT freed — stop this phase
            removed += 1; freed += int(b or 0)
            return True, True

        # Segments that failed the moov/trailer integrity check on close
        # (recorder.py, playable=0 -- see _mp4_has_moov()) are broken
        # footage: unplayable, and never becoming playable later. Purge
        # them unconditionally, ahead of and independent of retention/
        # quota/age -- there's no reason to keep a corrupted file around
        # just because it's young or the disk isn't full yet. Still
        # respects locked (a user protecting a segment wins even if it's
        # flagged broken) and stops the phase on the same "row dropped but
        # unlink failed" signal every other purge phase here uses.
        with self._lock:
            rows = self._db.execute(
                "SELECT id, bytes FROM segments WHERE playable = 0 AND locked = 0"
            ).fetchall()
        for sid, b in rows:
            _, keep_going = _delete_and_keep_going(sid, b)
            if not keep_going:
                break

        # Per-camera retention_days_override (0 = "use the global
        # default" — this was previously accepted/persisted by the
        # camera-add/update API and shown in the UI but never actually
        # read anywhere, a silent no-op). Queried as separate indexed
        # cutoff scans (global scan excluding overridden cameras, then
        # one small scan per overridden camera) rather than pulling every
        # segment into Python to filter by camera, so a purge tick stays
        # cheap regardless of how many segments exist in total. Not
        # scoped by root — retention is a footage-age rule, independent
        # of which disk a segment happens to live on.
        overrides: dict[str, int] = {}
        for c in self.config_store.get_cameras():
            try:
                ov = int(c.get("retention_days_override", 0) or 0)
            except (TypeError, ValueError):
                ov = 0
            if ov > 0:
                overrides[c["id"]] = ov

        def _purge_segments_older_than(days: int, cam_id: Optional[str]):
            cutoff = now_ts - days * 86400
            with self._lock:
                if cam_id is not None:
                    rows = self._db.execute(
                        "SELECT id, bytes FROM segments"
                        " WHERE ended_at < ? AND locked = 0 AND cam_id = ?",
                        (cutoff, cam_id)
                    ).fetchall()
                elif overrides:
                    placeholders = ",".join("?" * len(overrides))
                    rows = self._db.execute(
                        "SELECT id, bytes FROM segments"
                        f" WHERE ended_at < ? AND locked = 0 AND cam_id NOT IN ({placeholders})",
                        (cutoff, *overrides.keys())
                    ).fetchall()
                else:
                    rows = self._db.execute(
                        "SELECT id, bytes FROM segments WHERE ended_at < ? AND locked = 0",
                        (cutoff,)
                    ).fetchall()
            for sid, b in rows:
                _, keep_going = _delete_and_keep_going(sid, b)
                if not keep_going:
                    break

        if retention > 0:
            _purge_segments_older_than(retention, cam_id=None)
        for cam_id, days in overrides.items():
            _purge_segments_older_than(days, cam_id=cam_id)

        # Detections retention stays tied to the global default only —
        # retention_days_override is specifically a recording-footage
        # setting (matches its own name/UI copy), not a per-camera
        # detection-history one.
        if retention > 0:
            with self._lock:
                self._db.execute("DELETE FROM detections WHERE ended_at < ?", (now_ts - retention * 86400,))

        # Notification retention: independent of recording.retention_days
        # (notifications are a transient action log, not video). Note this
        # still only runs when purge_once() itself runs, which — like the
        # detections cleanup above — only happens while recording is
        # enabled (see _purge_loop); a pre-existing quirk, not new here.
        notif_cutoff = time.time() - NOTIF_RETENTION_DAYS * 86400
        with self._lock:
            self._db.execute("DELETE FROM notifications WHERE created_at < ?", (notif_cutoff,))
            self._db.execute(
                "DELETE FROM notifications WHERE id NOT IN "
                "(SELECT id FROM notifications ORDER BY created_at DESC LIMIT ?)",
                (NOTIF_MAX_ROWS,),
            )

        # Quota: max_gb=0 means unlimited. Global across every root (see
        # module docstring — no per-disk quota), so this stays a single
        # unscoped scan exactly like the single-root version always was.
        max_bytes = self.max_bytes()
        if max_bytes > 0:
            # Over-quota must be judged against the TRUE total (locked +
            # unlocked) — the same figure health()/pick_write_root() use
            # via _bytes_used() — not just what's eligible to delete. A
            # setup sitting on a lot of locked (protected) footage still
            # needs to be recognised as over its cap even though none of
            # that locked data can be freed here; getting this backwards
            # (summing only locked=0 rows) meant it could stay silently
            # over quota forever whenever enough usage happened to be
            # locked.
            over = self._bytes_used() - max_bytes
            if over > 0:
                with self._lock:
                    rows = self._db.execute(
                        "SELECT id, bytes FROM segments WHERE locked = 0 ORDER BY started_at ASC"
                    ).fetchall()
                for sid, b in rows:
                    if over <= 0: break
                    b = int(b or 0)
                    freed_ok, keep_going = _delete_and_keep_going(sid, b)
                    if freed_ok:
                        over -= b
                    if not keep_going:
                        break

        # Margin-based cleanup (deleting the oldest footage to make room
        # on a full disk) mainly runs synchronously at the moment that
        # actually needs the room — see free_up_for_new_segment(), called
        # from recorder.py right before every new segment/snapshot
        # starts. It's ALSO called here, once per tick, as a backstop:
        # that per-write trigger only fires while something is actively
        # writing. If recording is paused/disabled, or every camera is
        # off, nothing would otherwise ever notice a disk filling up from
        # some other cause (manual file copies, leftover snapshots) —
        # this periodic call is what still catches that.
        fu = self.free_up_for_new_segment()
        removed += fu["removed"]; freed += fu["freed_bytes"]

        # Snapshot retention: keep only 3× retention days for snapshots (they're small)
        snap_retention = max(retention * 3, 30)
        if snap_retention > 0:
            cutoff = time.time() - snap_retention * 86400
            with self._lock:
                old_snaps = self._db.execute(
                    "SELECT id, path FROM snapshots WHERE taken_at < ?", (cutoff,)
                ).fetchall()
            for sid, p in old_snaps:
                try: Path(p).unlink(missing_ok=True)
                except Exception: pass
                with self._lock:
                    self._db.execute("DELETE FROM snapshots WHERE id = ?", (sid,))

        # No-op unless auto_vacuum=INCREMENTAL was set at creation time
        # (_ensure_roots only does that for a brand-new database — an
        # existing one keeps whatever mode it already had, see there for
        # why). SQLite documents incremental_vacuum as a no-op when
        # auto_vacuum isn't enabled, but NOT as a no-op just because
        # nothing was actually freed this tick — it still does real
        # (if usually small) disk I/O to walk/rewrite the free-page list,
        # while holding self._lock, the same lock every other Storage
        # method needs (list_segments, stats(), health(), notifications,
        # playback…). Unconditionally on every purge tick (default every
        # 60s, whether or not anything was deleted) that I/O landing badly
        # on SD-card storage was a real, reported cause of the whole app
        # intermittently stalling for several seconds. Only run it when
        # this tick actually deleted something — most ticks delete
        # nothing — and log slow ones so a stall like this is diagnosable
        # from journalctl instead of invisible.
        if removed:
            try:
                t0 = time.time()
                with self._lock:
                    self._db.execute("PRAGMA incremental_vacuum(200)")
                dt = time.time() - t0
                if dt > 1.0:
                    log.warning("incremental_vacuum held storage lock for %.2fs", dt)
            except Exception as e:
                log.debug("incremental_vacuum failed: %s", e)

        if removed:
            log.info("purged %d segments (%.2f MB freed)", removed, freed / 1e6)
        return {"removed": removed, "freed_bytes": freed}

    # ---------- Rescan (index rebuild from disk) ----------
    def rescan(self, progress_cb=None) -> dict:
        """Walk every configured root and register any MP4 not already in
        the index, then drop any indexed row whose file no longer exists
        on disk (deleted outside the app — manual rm, an external script,
        a swapped disk). This is the manual "Diski yeniden tara" action
        from Ayarlar (also run automatically by purge_once() to recover a
        suspected orphan); nothing else currently reconciles a file
        removed that way.

        Batches its DB work rather than one round-trip per file: on an
        archive with tens of thousands of segments, checking each file's
        existence with its own SELECT — and each new file with its own
        autocommit INSERT, each serialized on self._lock — made a full
        rescan far slower than the actual disk I/O required, and held
        self._lock (shared with almost every other Storage read) for a
        long string of tiny transactions instead of a few big ones.

        progress_cb, when given, is called with keyword updates
        (phase/total/scanned/added) as scanning proceeds, so a caller
        (start_rescan_async) can expose live progress instead of this
        being an opaque multi-minute black box on a large archive.
        """
        from app.recorder import _parse_fname_ts   # local import to avoid cycle
        # Snapshots can now land under ANY configured root's own
        # "_snapshots" subdir (see pick_snapshot_root()), not just the
        # primary root's — exclude all of them, not just one.
        snap_roots = {sr.resolve() for sr in self.snapshot_roots()}
        roots = [r.resolve() for r in self.roots()]

        def _files():
            for root in roots:
                for p in root.rglob("*.mp4"):
                    if not p.is_file(): continue
                    try:
                        if any(sr in p.parents for sr in snap_roots): continue
                    except Exception:
                        pass
                    yield root, p

        # A quick first pass just to get a real denominator for progress
        # (no stat()/DB work, just directory listing) — doubles the
        # directory-read I/O but that's cheap next to the stat+DB work
        # below, and a real percentage is the whole point of this vs. the
        # old fire-and-forget button.
        if progress_cb: progress_cb(phase="counting")
        total = sum(1 for _ in _files())
        if progress_cb: progress_cb(phase="scanning", total=total)

        # Load every already-indexed path once instead of one SELECT per
        # file.
        with self._lock:
            existing = {row[0] for row in self._db.execute("SELECT path FROM segments").fetchall()}

        new_rows: list[tuple] = []

        def _flush():
            if not new_rows: return
            with self._lock:
                self._db.execute("BEGIN")
                try:
                    self._db.executemany(
                        "INSERT OR IGNORE INTO segments"
                        " (cam_id, path, started_at, ended_at, duration, bytes, trigger, playable)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        new_rows,
                    )
                    self._db.execute("COMMIT")
                except Exception:
                    self._db.execute("ROLLBACK")
                    raise
            new_rows.clear()

        found = 0; added = 0
        for root, p in _files():
            found += 1
            abs_p = str(p.resolve())
            if abs_p not in existing:
                try:
                    rel = p.relative_to(root)
                    cam_id = rel.parts[0] if rel.parts else "unknown"
                except ValueError:
                    cam_id = "unknown"
                try:
                    st = p.stat()
                except OSError:
                    st = None
                if st is not None:
                    fts = _parse_fname_ts(p)
                    started = fts if fts is not None else max(0.0, st.st_mtime - 1)
                    duration = max(0.0, st.st_mtime - started)
                    # trigger="rescan", playable defaults to 1 (only ever
                    # flagged 0 on a positive, verified failure — see
                    # register_segment) — matches its normal behavior.
                    new_rows.append((cam_id, abs_p, started, st.st_mtime, duration, st.st_size, "rescan", 1))
                    added += 1
                    if len(new_rows) >= 200:
                        _flush()
            if progress_cb and found % 25 == 0:
                progress_cb(scanned=found, added=added)
        _flush()
        if progress_cb: progress_cb(scanned=found, added=added, phase="cleaning")

        with self._lock:
            all_paths = self._db.execute("SELECT id, path FROM segments").fetchall()
        orphaned = [sid for sid, p in all_paths if not os.path.exists(p)]
        if orphaned:
            with self._lock:
                self._db.execute("BEGIN")
                try:
                    self._db.executemany("DELETE FROM segments WHERE id = ?",
                                         [(sid,) for sid in orphaned])
                    self._db.execute("COMMIT")
                except Exception:
                    self._db.execute("ROLLBACK")
                    raise
            log.info("rescan: dropped %d segment row(s) whose file no longer exists",
                     len(orphaned))

        if added or orphaned:
            self._invalidate_caches()

        result = {"scanned": found, "added": added, "removed": len(orphaned)}
        if progress_cb: progress_cb(**result)
        return result

    def start_rescan_async(self) -> dict:
        """Kick off rescan() on a background thread and return immediately.
        A full disk walk can take minutes on a large archive over slow
        SD-card/eMMC storage — running it inline in the request handler
        meant the "Diski yeniden tara" button either looked like it did
        nothing (no feedback while it silently ran) or, now that every
        frontend fetch is bounded (see app.js's API_TIMEOUT_MS), would
        abort with a false "başarısız" after 8s while the scan kept going
        server-side regardless. Callers should poll get_rescan_status()."""
        with self._rescan_lock:
            if self._rescan_state["running"]:
                return dict(self._rescan_state)
            self._rescan_state = {
                "running": True, "phase": "counting", "total": 0, "scanned": 0,
                "added": 0, "removed": 0, "done": False, "ok": None, "error": None,
                "started_at": time.time(), "finished_at": None,
            }
        threading.Thread(target=self._rescan_worker, daemon=True, name="rtcview-rescan").start()
        with self._rescan_lock:
            return dict(self._rescan_state)

    def get_rescan_status(self) -> dict:
        with self._rescan_lock:
            return dict(self._rescan_state)

    def _rescan_progress(self, **kw):
        with self._rescan_lock:
            self._rescan_state.update(kw)

    def _rescan_worker(self):
        try:
            result = self.rescan(progress_cb=self._rescan_progress)
            self._rescan_progress(running=False, done=True, ok=True,
                                   phase="done", finished_at=time.time(), **result)
        except Exception as e:
            log.exception("async rescan failed")
            self._rescan_progress(running=False, done=True, ok=False,
                                   phase="error", error=str(e), finished_at=time.time())


def _try_prune_empty(dir_path: Path, root: Path):
    """After a file delete, remove empty date/cam subdirs up to (not including) root."""
    try:
        root = root.resolve()
        cur = dir_path.resolve()
        while cur != root and root in cur.parents:
            if any(cur.iterdir()): return
            cur.rmdir()
            cur = cur.parent
    except Exception:
        pass
