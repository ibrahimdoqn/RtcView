"""Recording storage: SQLite segment index + retention/quota purger.

Single-disk by design: RtcView records to exactly ONE storage root, which
the admin mounts and points the app at (see README's Kurulum). RtcView
itself never formats, mounts, or manages disks — that's deliberately kept
out of this app's blast radius (a previous multi-disk auto-management
feature was removed after a flaky external-USB drive it was recording to
went into a hardware failure loop, and the app's own automated per-disk
purge logic amplified the resulting mess). One disk, chosen and mounted
by a human, is a much smaller and easier thing to reason about and keep
healthy.
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

# Physical free space below which the disk is considered "no room". Both
# the write picker and the fullness purge respect this — ffmpeg needs
# some slack to flush its buffer without hitting ENOSPC mid-segment.
SAFETY_MARGIN_BYTES = 1 * 1024**3  # 1 GB

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


class Storage:
    """
    Thread-safe SQLite index for recording segments and snapshots plus a
    background purger enforcing retention (days) and quota (bytes) against
    a single storage root.

    index.sqlite itself lives in the app's config directory (next to
    config.json), NOT under the recording storage root. It used to live at
    root()/index.sqlite, which meant changing the storage path in Kayıt &
    Depolama settings — an ordinary thing to do when switching disks —
    opened a brand-new empty database at the new location: every
    previously indexed segment vanished from the UI (playback, stats,
    health) even though every video file was untouched on disk. The index
    is app STATE, not recording data, and its location must not depend on
    which path the admin currently points recording at.
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
        # on register/delete/set_root.
        self._disk_cache: Optional[tuple[float, dict]] = None
        self._stats_cache: Optional[tuple[float, dict]] = None
        self._health_cache: Optional[tuple[float, dict]] = None
        # Set when delete_segment() drops an index row but the file itself
        # fails to unlink (transient I/O hiccup, disk briefly unwritable) —
        # the file is now an untracked orphan still taking up space. The
        # purge loop rescans on its next tick to re-register it so it's
        # visible (and purgeable) again, instead of silently sitting there
        # forever until someone notices and clicks "Diski yeniden tara".
        self._orphan_suspected = False
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
        self._ensure_root()

    # ---------- Root / DB ----------
    def _rec_cfg(self):
        return self.config_store.get_recording()

    def root(self) -> Path:
        """The single configured storage root, resolved."""
        rc = self._rec_cfg()
        p = str(rc.get("storage_path") or "/opt/rtcview/recordings")
        return Path(p).expanduser().resolve()

    def max_bytes(self) -> int:
        try:
            gb = int(self._rec_cfg().get("max_gb", 0) or 0)
        except (TypeError, ValueError):
            gb = 0
        return max(0, gb) * (1024**3)

    def snapshots_root(self) -> Path:
        return self.root() / "_snapshots"

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
        """SUM(bytes) for every segment under the storage root.

        Computed in SQL — a LIKE 'prefix%' pattern lets SQLite range-scan
        the UNIQUE index already on path — rather than pulling every
        segment row into Python and filtering with str.startswith.
        """
        pattern = self._like_escape(str(self.root()) + os.sep) + "%"
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM segments WHERE path LIKE ? ESCAPE '\\'",
                (pattern,)
            ).fetchone()
        return int(row[0] or 0)

    def _has_purgeable_segments(self) -> bool:
        """Whether ANY unlocked segment exists. health() uses this to tell
        "disk is at capacity, and is being actively kept there by the
        rolling purge" (expected steady state for an unlimited-quota root
        — not an error) apart from "disk is full AND there's nothing left
        to delete" (an actual dead end — recording will start failing)."""
        with self._lock:
            row = self._db.execute("SELECT 1 FROM segments WHERE locked = 0 LIMIT 1").fetchone()
        return row is not None

    def pick_write_root(self) -> Path:
        """Return the storage root, creating it if needed. Kept as its own
        method (rather than inlining `root()` at call sites) because
        recorder.py calls this once per recording-session start — the name
        describes the historical multi-root API more than what it does
        now, but changing every caller wasn't worth it for a one-line
        method."""
        r = self.root()
        try:
            r.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("Cannot create storage root %s: %s", r, e)
        return r

    def _ensure_root(self):
        try:
            self.root().mkdir(parents=True, exist_ok=True)
            self.snapshots_root().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("Cannot create storage root: %s", e)
            return
        # Stable regardless of storage_path — see the class docstring.
        # config_path's parent is guaranteed to exist already (ConfigStore
        # creates it), mkdir here is just defensive.
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
                # moved out of the recording root have their real history
                # sitting at <root>/index.sqlite. Move it (with its
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
        old_path = self.root() / "index.sqlite"
        if old_path == new_path or not old_path.exists():
            return
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(old_path) + suffix)
                if src.exists():
                    shutil.move(str(src), str(new_path) + suffix)
            log.info("Migrated recording index from %s to %s", old_path, new_path)
        except Exception as e:
            log.warning("Could not migrate legacy index from %s: %s", old_path, e)

    def set_root(self, new_path: str, max_gb: Optional[int] = None) -> tuple[bool, str]:
        """Change the storage root. The new path must be creatable and
        writable. max_gb is optional — pass None to leave the quota
        unchanged."""
        s = str(new_path or "").strip()
        if not s:
            return False, "Kayıt klasörü boş olamaz"
        p = Path(s).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"Klasör oluşturulamadı: {p} ({e})"
        probe = p / ".rtcview_write_test"
        try:
            probe.write_text("ok"); probe.unlink()
        except Exception as e:
            return False, f"Klasöre yazılamıyor: {p} ({e})"
        rp = str(p.resolve())
        updates = {"storage_path": rp}
        if max_gb is not None:
            try:
                updates["max_gb"] = max(0, int(max_gb))
            except (TypeError, ValueError):
                pass
        self.config_store.update_recording(updates)
        self._ensure_root()
        self._invalidate_caches()
        return True, rp

    def _invalidate_caches(self):
        self._stats_cache = None
        self._health_cache = None
        self._disk_cache = None

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

    def delete_segment(self, seg_id: int, force: bool = False) -> bool:
        seg = self.get_segment(seg_id)
        if not seg: return False
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
            return False
        # Unlink AFTER the row is confirmed gone (and confirmed not
        # locked, atomically, per above). If the OS refuses the unlink
        # (permission, ENOENT, a disk so full even deleting fails), the
        # index row is already dropped — self._orphan_suspected flags it
        # so the next purge tick rescans and re-registers the file (still
        # physically on disk) so it isn't silently forgotten.
        try:
            Path(seg["path"]).unlink(missing_ok=True)
        except Exception as e:
            log.warning("delete_segment: unlink failed %s: %s", seg["path"], e)
            self._orphan_suspected = True
        # Invalidate everything: the reclaim/write-picker logic relies on
        # fresh bytes_used AND disk_usage numbers.
        self._invalidate_caches()
        # Prune empty date/cam subdirs up to (not including) the root.
        parent = Path(seg["path"]).parent
        try:
            _try_prune_empty(parent, self.root())
        except Exception:
            pass
        return True

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
        """Report the storage subsystem's live state.

        Cached 15 s so a 4-s polling client doesn't fire a real
        write-test / statfs on every hit. Cache is invalidated by
        set_root() and by segment register/delete.

        status: "ok" | "warning" | "error"
        """
        now = time.time()
        cached = self._health_cache
        if cached and (now - cached[0]) < 15:
            return cached[1]

        errors: list[str] = []
        warnings: list[str] = []
        r = self.root()
        exists = r.exists()
        writable = False
        disk = {"total": 0, "free": 0, "used": 0}
        free_pct = 100.0
        if not exists:
            errors.append(f"Klasör yok: {r}")
        else:
            # Disk usage first — the write-test error below needs to know
            # whether free space is already critically low, since some
            # filesystems (f2fs in particular, once it runs out of free
            # segments for a checkpoint) return EIO instead of ENOSPC for
            # what is still fundamentally a "disk is full" condition, not
            # a permissions/hardware fault.
            du = None
            try:
                du = shutil.disk_usage(str(r))
                disk = {"total": du.total, "free": du.free, "used": du.used}
                free_pct = (du.free / du.total) * 100 if du.total else 0
            except Exception as e:
                errors.append(f"Disk okunamadı: {e}")
            near_full = du is not None and du.free < 1 * 1024**3
            # Unique per call (pid+thread), not a fixed name: health() is
            # cached for 15s but that cache check-then-set isn't atomic,
            # so a burst of concurrent callers (e.g. several browser polls
            # queued while a mobile tab was backgrounded all firing at
            # once on resume) can each decide to run a fresh write-test at
            # the same moment. With a shared fixed filename, one caller's
            # unlink() removing the file before another caller's own
            # unlink() runs raised a genuine FileNotFoundError that got
            # reported to the user as "Yazılamıyor: ..." even though
            # nothing was actually wrong with the disk.
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
                    # A disk this full — whether the write-test hit ENOSPC
                    # directly or disk_usage() already shows it under the
                    # 1 GB margin — is the SAME "disk kapasitesine yakın"
                    # story: the rolling/retention purge doesn't care how
                    # it got full, only that something old enough still
                    # exists to reclaim. A write failure while there's
                    # PLENTY of free space is NOT something purging can
                    # ever fix (wrong permissions, read-only fs, hardware
                    # fault), so that stays a hard error, unconditionally.
                    if not self._has_purgeable_segments():
                        errors.append(
                            "Yazılamıyor: disk dolu — silinecek kayıt kalmadı, kayıt durabilir"
                        )
                else:
                    errors.append(f"Yazılamıyor: {write_err}")
            elif near_full and not self._has_purgeable_segments():
                # Sitting right at capacity is the EXPECTED steady state
                # for an unlimited-quota (max_gb=0) root once footage
                # volume outgrows the disk — purge_once() keeps deleting
                # the oldest segments to hold it just above
                # SAFETY_MARGIN_BYTES, forever, by design (a rolling
                # buffer bounded by physical disk size rather than by
                # retention_days alone). Not worth flagging at all while
                # that's still working (stays "ok"/green) — only a
                # genuine dead end, nothing left to purge, is actionable.
                errors.append(
                    f"Disk doldu: {du.free // (1024*1024)} MB boş — "
                    "silinecek kayıt kalmadı, kayıt durabilir"
                )
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

        overall = "error" if errors else ("warning" if warnings else "ok")
        result = {
            "status": overall,
            "root": str(r),
            "exists": exists,
            "writable": writable,
            "db_ok": db_ok,
            "disk": disk,
            "free_percent": round(free_pct, 1),
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

        dc = self._disk_cache
        if dc and (now_ts - dc[0]) < 5:
            disk = dc[1]
        else:
            try:
                du = shutil.disk_usage(str(self.root()))
                disk = {"total": du.total, "free": du.free, "used": du.used}
            except Exception:
                disk = {"total": 0, "free": 0, "used": 0}
            self._disk_cache = (now_ts, disk)

        rc = self._rec_cfg()
        max_bytes = self.max_bytes()
        result = {
            "root": str(self.root()),
            "bytes_used": int(total_bytes),
            "segment_count": int(total_count),
            # None when there are no segments at all yet, otherwise the
            # started_at of the single oldest segment across every camera
            # — lets the Kayıt & Depolama tab show exactly how far back
            # current footage goes, which is also a direct, concrete
            # answer to "when will retention start freeing up space"
            # (once this timestamp crosses retention_days).
            "oldest_started_at": float(oldest_started_at) if oldest_started_at is not None else None,
            "max_gb": max_bytes // (1024**3) if max_bytes else 0,
            "max_bytes": max_bytes,
            "retention_days": int(rc.get("retention_days", 14)),
            "disk": disk,
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
            fails — observed in production on f2fs): every purge phase
            below must stop right there instead of ploughing through its
            whole remaining batch/table, since continuing only turns more
            of the root's tracked history into untracked orphans for zero
            space gained. The next tick's auto-rescan (top of this
            function) recovers whatever got orphaned this way.

            freed_ok is True only when the file was genuinely deleted, so
            callers doing their own byte accounting (the quota loop's
            running `over`) don't have to re-derive success."""
            nonlocal removed, freed
            if not self.delete_segment(sid):
                return False, True   # already gone/locked — not a failure, keep going
            if self._orphan_suspected:
                return False, False  # row dropped, file NOT freed — stop this phase
            removed += 1; freed += int(b or 0)
            return True, True

        # Per-camera retention_days_override (0 = "use the global
        # default" — this was previously accepted/persisted by the
        # camera-add/update API and shown in the UI but never actually
        # read anywhere, a silent no-op). Queried as separate indexed
        # cutoff scans (global scan excluding overridden cameras, then
        # one small scan per overridden camera) rather than pulling every
        # segment into Python to filter by camera, so a purge tick stays
        # cheap regardless of how many segments exist in total.
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

        # Quota: max_gb=0 means unlimited.
        max_bytes = self.max_bytes()
        if max_bytes > 0:
            # Over-quota must be judged against the TRUE total (locked +
            # unlocked) — the same figure health()/pick_write_root() use
            # via _bytes_used() — not just what's eligible to delete. A
            # root sitting on a lot of locked (protected) footage still
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

        # Rolling-buffer reclaim: once the disk itself drops under the
        # safety margin — regardless of quota, regardless of
        # retention_days — age the oldest unlocked segments out until
        # there's room again. This is what keeps a disk without a quota
        # (or one whose footage volume outgrows retention_days) from
        # running itself into the ground: physical capacity is always the
        # backstop.
        #
        # Ground truth is shutil.disk_usage(), not the write-test's errno:
        # some filesystems (f2fs once it runs out of free segments for a
        # checkpoint, in particular) return EIO instead of ENOSPC for what
        # is still fundamentally "disk is full" — but a low disk_usage()
        # free reading means the same thing regardless of which errno the
        # filesystem happened to surface.
        try:
            free = shutil.disk_usage(str(self.root())).free
        except Exception:
            free = None
        if free is not None and free < SAFETY_MARGIN_BYTES:
            log.warning("reclaim: storage root below safety margin (%d MB free)", free // (1024*1024))
            BATCH = 5
            while not self._stop.is_set():
                with self._lock:
                    batch = self._db.execute(
                        "SELECT id, bytes FROM segments"
                        " WHERE locked = 0 ORDER BY started_at ASC LIMIT ?",
                        (BATCH,)
                    ).fetchall()
                if not batch:
                    log.error("reclaim: nothing left to delete — all segments locked")
                    break
                stop_phase = False
                for sid, b in batch:
                    _, keep_going = _delete_and_keep_going(sid, b)
                    if not keep_going:
                        stop_phase = True
                        break
                if stop_phase:
                    log.warning("reclaim: a delete stopped freeing real space — pausing this tick")
                    break
                try:
                    if shutil.disk_usage(str(self.root())).free >= SAFETY_MARGIN_BYTES:
                        break
                except Exception:
                    break

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
        # (_ensure_root only does that for a brand-new database — an
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
        """Walk the storage root and register any MP4 not already in the
        index, then drop any indexed row whose file no longer exists on
        disk (deleted outside the app — manual rm, an external script).
        This is the manual "Diski yeniden tara" action from Ayarlar (also
        run automatically by purge_once() to recover a suspected orphan);
        nothing else currently reconciles a file removed that way.

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
        snap_root = self.snapshots_root().resolve()
        root = self.root()

        def _files():
            for p in root.rglob("*.mp4"):
                if not p.is_file(): continue
                try:
                    if snap_root in p.parents: continue
                except Exception:
                    pass
                yield p

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
        for p in _files():
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
