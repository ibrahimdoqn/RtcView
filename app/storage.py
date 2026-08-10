"""Recording storage: SQLite segment index + retention/quota purger."""
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

# Physical free space below which a disk is considered "no room". Both
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
    kind       TEXT    NOT NULL,   -- 'motion' | 'person'
    started_at REAL    NOT NULL,
    ended_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_det_cam_time ON detections(cam_id, started_at);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cam_id     TEXT    NOT NULL,
    kind       TEXT    NOT NULL,   -- 'motion' | 'person'
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
    background purger enforcing retention (days) and quota (bytes).

    All paths are stored absolute. The storage root can change at runtime
    (see `set_root`) — new segments go under the new root; the index still
    resolves old segments by their stored absolute path.

    index.sqlite itself lives in the app's config directory (next to
    config.json), NOT under a recording storage root. It used to live at
    primary_root()/index.sqlite, which meant reordering storage_paths in
    Kayıt & Depolama settings — an ordinary thing to do when a disk fills
    up and another should take over — opened a brand-new empty database
    at the new primary's location: every previously indexed segment
    vanished from the UI (playback, stats, health) even though every
    video file was untouched on every disk. The index is app STATE, not
    recording data, and its location must not depend on which disk the
    admin currently prefers.
    """

    def __init__(self, config_store):
        self.config_store = config_store
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        self._db_path: Optional[Path] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Cached derived state — a 4-s status poll shouldn't fire a real
        # write-test / statfs / SQL aggregate on every hit. Invalidated
        # opportunistically on register/delete for the stats one.
        self._disk_cache: dict[str, tuple[float, dict]] = {}   # per-root
        self._stats_cache: Optional[tuple[float, dict]] = None
        self._health_cache: Optional[tuple[float, dict]] = None
        self._ensure_roots()

    # ---------- Root / DB ----------
    def _rec_cfg(self):
        return self.config_store.get_recording()

    def roots(self) -> list[Path]:
        """All configured storage roots, in preference order. Missing
        entries and blanks are dropped; loopback tildes expanded."""
        return [Path(r["path"]) for r in self.roots_with_quota()]

    def roots_with_quota(self) -> list[dict]:
        """Same order as roots(), each element = {"path": str (resolved),
        "max_gb": int, "max_bytes": int}. max_gb=0 means unlimited for
        that specific disk."""
        rc = self._rec_cfg()
        raw = list(rc.get("storage_paths") or [])
        if not raw and rc.get("storage_path"):
            raw = [rc["storage_path"]]
        out: list[dict] = []
        seen: set = set()
        for r in raw:
            if not r: continue
            if isinstance(r, dict):
                path = r.get("path")
                max_gb = int(r.get("max_gb", 0) or 0)
            else:
                path = r; max_gb = 0
            if not path: continue
            p = Path(str(path)).expanduser().resolve()
            if p in seen: continue
            seen.add(p)
            out.append({"path": str(p), "max_gb": max_gb, "max_bytes": max_gb * (1024**3)})
        return out

    def primary_root(self) -> Path:
        rs = self.roots()
        return rs[0] if rs else Path("/opt/rtcview/recordings").resolve()

    # Kept for legacy call-sites (rescan / etc.). Returns the primary.
    def root(self) -> Path:
        return self.primary_root()

    def snapshots_root(self) -> Path:
        return self.primary_root() / "_snapshots"

    @staticmethod
    def _like_escape(s: str) -> str:
        """Escape % / _ / \\ so a path can be used as a literal prefix in
        a SQL LIKE pattern (paired with ESCAPE '\\' at the call site). A
        root path containing either wildcard character — not exotic,
        e.g. /mnt/disk_1 or /media/usb_drive — would otherwise silently
        widen the match to unrelated paths that merely share the same
        prefix up to that character."""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _bytes_used_under(self, root_path: str) -> int:
        """SUM(bytes) for segments whose path is under root_path.

        Computed in SQL — a LIKE 'prefix%' pattern lets SQLite range-scan
        the UNIQUE index already on path — rather than pulling every
        segment row into Python and filtering with str.startswith, which
        is what pick_write_root/health/stats used to each do independently
        on every call. That full scan grows linearly with the whole
        segments table (every segment ever recorded, not just this root's)
        and ran under self._lock, so it also serialized every other DB
        operation for however long it took.
        """
        pattern = self._like_escape(root_path + os.sep) + "%"
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM segments WHERE path LIKE ? ESCAPE '\\'",
                (pattern,)
            ).fetchone()
        return int(row[0] or 0)

    def _has_purgeable_segments(self) -> bool:
        """Whether ANY unlocked segment exists anywhere (checked globally,
        not per-root, since the emergency purge itself deletes the
        globally-oldest segment regardless of which root it's on).
        health() uses this to tell "disk is at capacity, and is being
        actively kept there by the rolling purge" (expected steady state
        for an unlimited-quota root — not an error) apart from "disk is
        full AND there's nothing left to delete" (an actual dead end —
        recording will start failing)."""
        with self._lock:
            row = self._db.execute("SELECT 1 FROM segments WHERE locked = 0 LIMIT 1").fetchone()
        return row is not None

    def pick_write_root(self) -> Path:
        """Sequential fill: try each configured root in order.

        Return the FIRST root that has room right now — where "room"
        means (writable) AND (under its per-disk quota, if any) AND
        (physical free space >= SAFETY_MARGIN). A root that is unmounted
        or over its cap is skipped, and the next one down the list is
        tried. This is the "önce A dolsun, sonra B" model: as long as
        A has room, every write goes to A. When A finally can't fit
        another safety-buffered write, B takes over.

        Fallback: if every root is out of room, return whichever is
        merely writable so the current segment still lands somewhere.
        purge_once will free space on its next tick — the emergency
        purge path in there deletes the globally-oldest segments until
        at least one root becomes writable again.
        """
        fallback = None
        for entry in self.roots_with_quota():
            r = Path(entry["path"])
            try:
                r.mkdir(parents=True, exist_ok=True)
                probe = r / ".rtcview_write_test"
                probe.write_text("ok"); probe.unlink()
                du = shutil.disk_usage(str(r))
            except Exception:
                continue
            fallback = r
            if du.free < SAFETY_MARGIN_BYTES:
                continue                    # physically no room
            if entry["max_bytes"] > 0:
                if self._bytes_used_under(entry["path"]) >= entry["max_bytes"]:
                    continue                # per-disk quota hit
            return r                        # first eligible wins
        return fallback if fallback else self.primary_root()

    def _ensure_roots(self):
        try:
            for r in self.roots():
                r.mkdir(parents=True, exist_ok=True)
            self.snapshots_root().mkdir(parents=True, exist_ok=True)
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
        """Replace the entire storage_paths list.

        Accepts either the new schema (list of {"path", "max_gb"}) or
        the legacy string list (each becomes {"path": s, "max_gb": 0}).
        Every entry's path must be creatable + writable.
        """
        if not new_paths:
            return False, "En az bir kayıt klasörü olmalı"
        clean: list[dict] = []
        seen: set = set()
        for np in new_paths:
            if isinstance(np, dict):
                s = str(np.get("path") or "").strip()
                try: quota = max(0, int(np.get("max_gb", 0) or 0))
                except (TypeError, ValueError): quota = 0
            else:
                s = str(np or "").strip()
                quota = 0
            if not s: continue
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
            if rp in seen: continue
            seen.add(rp)
            clean.append({"path": rp, "max_gb": quota})
        if not clean:
            return False, "Geçerli klasör bulunamadı"
        # Keep the legacy scalar in sync with the new primary so a
        # downgrade doesn't silently forget the recording path.
        self.config_store.update_recording({
            "storage_paths": clean,
            "storage_path": clean[0]["path"],
        })
        self._ensure_roots()
        self._invalidate_caches()
        return True, ", ".join(f"{c['path']}={c['max_gb']}GB" if c['max_gb'] else c['path'] for c in clean)

    def set_root(self, new_path: str) -> tuple[bool, str]:
        """Legacy single-path API used by older /api/recording/settings
        callers. Just wraps set_roots with a single-element list."""
        return self.set_roots([{"path": new_path, "max_gb": 0}])

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
        # A new segment changes per-root bytes_used (quota/health
        # decisions consume it) and disk usage (the file just landed).
        # Invalidate every cache, not just stats — a stale health cache
        # would leave the emergency-purge check working from old numbers.
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
        # (permission, ENOENT), the index row is already dropped so a
        # fresh rescan can normalise the state; the file — if it still
        # exists — will be re-registered as a new id.
        try:
            Path(seg["path"]).unlink(missing_ok=True)
        except Exception as e:
            log.warning("delete_segment: unlink failed %s: %s", seg["path"], e)
        # Invalidate everything: emergency purge / write picker rely on
        # fresh per-root byte totals AND disk_usage numbers.
        self._invalidate_caches()
        # Prune empty date/cam dirs up to whichever configured root
        # this file lived under (works with multi-root layouts).
        parent = Path(seg["path"]).parent
        for r in self.roots():
            try:
                if r in parent.parents or r == parent:
                    _try_prune_empty(parent, r); break
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
        """Report the storage subsystem's live state across ALL roots.

        Cached 15 s so a 4-s polling client doesn't fire a real
        write-test / statfs on every hit. Cache is invalidated by
        set_roots() and by segment register/delete.

        Overall status = worst-of-any-root:
          "ok" | "warning" | "error"
        """
        now = time.time()
        cached = self._health_cache
        if cached and (now - cached[0]) < 15:
            return cached[1]

        errors: list[str] = []
        warnings: list[str] = []
        per_root = []
        overall = "ok"
        for entry in self.roots_with_quota():
            r = Path(entry["path"])
            r_errs = []; r_warns = []
            exists = r.exists()
            writable = False
            disk = {"total": 0, "free": 0, "used": 0}
            free_pct = 100.0
            if not exists:
                r_errs.append(f"Klasör yok: {r}")
            else:
                test = r / ".rtcview_write_test"
                try:
                    test.write_text("ok"); test.unlink()
                    writable = True
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        # A disk so full even the tiny write-test probe
                        # can't land is the SAME "disk kapasitesine yakın"
                        # story as the free-space check below, just past
                        # zero bytes instead of under the 1 GB margin —
                        # the rolling/retention purge doesn't care how it
                        # got full, only that something old enough still
                        # exists to reclaim. A real problem here (wrong
                        # permissions, read-only fs, hardware fault) is
                        # NOT something purging can ever fix, so anything
                        # other than ENOSPC stays a hard error below,
                        # unconditionally.
                        if not self._has_purgeable_segments():
                            r_errs.append(
                                "Yazılamıyor: disk dolu — silinecek kayıt kalmadı, kayıt durabilir"
                            )
                    else:
                        r_errs.append(f"Yazılamıyor: {e}")
                except Exception as e:
                    r_errs.append(f"Yazılamıyor: {e}")
                try:
                    du = shutil.disk_usage(str(r))
                    disk = {"total": du.total, "free": du.free, "used": du.used}
                    free_pct = (du.free / du.total) * 100 if du.total else 0
                    if du.free < 1 * 1024**3 and not self._has_purgeable_segments():
                        # A root sitting right at capacity is the EXPECTED
                        # steady state for an unlimited-quota (max_gb=0)
                        # root once its footage volume outgrows the disk —
                        # the emergency purge in purge_once() keeps deleting
                        # the oldest segments to hold this root just above
                        # SAFETY_MARGIN_BYTES, forever, by design (a rolling
                        # buffer bounded by physical disk size rather than
                        # by retention_days alone). Not worth flagging at
                        # all while that's still working (stays "ok"/green)
                        # — only a genuine dead end, nothing left anywhere
                        # to purge, is actually actionable.
                        r_errs.append(
                            f"Disk doldu: {du.free // (1024*1024)} MB boş — "
                            "silinecek kayıt kalmadı, kayıt durabilir"
                        )
                except Exception as e:
                    r_errs.append(f"Disk okunamadı: {e}")
            used_by_rec = self._bytes_used_under(entry["path"])
            if entry["max_bytes"] > 0:
                q_pct = round((used_by_rec / entry["max_bytes"]) * 100, 1)
                if q_pct >= 100 and overall != "error":
                    r_warns.append(f"Kota dolu ({entry['max_gb']} GB)")
                elif q_pct >= 90:
                    r_warns.append(f"Kota %{q_pct:.0f}")
            rst = "error" if r_errs else ("warning" if r_warns else "ok")
            per_root.append({
                "path": str(r), "status": rst, "exists": exists, "writable": writable,
                "disk": disk, "free_percent": round(free_pct, 1),
                "bytes_used": used_by_rec,
                "max_gb": entry["max_gb"], "max_bytes": entry["max_bytes"],
                "errors": r_errs, "warnings": r_warns,
            })
            for e in r_errs: errors.append(f"{r}: {e}")
            for w in r_warns: warnings.append(f"{r}: {w}")
            if rst == "error" or (rst == "warning" and overall == "ok"):
                overall = rst
        # DB itself
        db_ok = False
        try:
            with self._lock:
                self._db.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception as e:
            errors.append(f"DB erişim: {e}")
            overall = "error"

        primary = self.primary_root()
        prim = next((x for x in per_root if x["path"] == str(primary)), None)
        # Top-level disk = SUM across all configured roots so multi-disk
        # setups report combined free/total (matches what stats() does).
        agg = {"total": 0, "free": 0, "used": 0}
        for pr in per_root:
            for k in ("total", "free", "used"):
                agg[k] += pr["disk"].get(k, 0)
        agg_free_pct = round((agg["free"] / agg["total"]) * 100, 1) if agg["total"] else 0
        result = {
            "status": overall,
            "root": str(primary),               # legacy field for UI compat
            "roots": per_root,                  # per-root breakdown
            "exists": bool(prim and prim["exists"]),
            "writable": bool(prim and prim["writable"]),
            "db_ok": db_ok,
            "disk": agg,                        # aggregate across all roots
            "free_percent": agg_free_pct,       # aggregate percentage
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
            total_bytes, total_count = self._db.execute(
                "SELECT COALESCE(SUM(bytes),0), COUNT(*) FROM segments"
            ).fetchone()
            per_cam = self._db.execute(
                "SELECT cam_id, COALESCE(SUM(bytes),0), COUNT(*),"
                " MIN(started_at), MAX(ended_at)"
                " FROM segments GROUP BY cam_id"
            ).fetchall()

        # Per-root usage from the segment index (root prefix match).
        roots_q = self.roots_with_quota()
        per_root_used: dict[str, int] = {r["path"]: self._bytes_used_under(r["path"]) for r in roots_q}

        # disk usage per configured root, cached individually (5 s)
        roots_stats = []
        agg = {"total": 0, "free": 0, "used": 0}
        for r in roots_q:
            key = r["path"]
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
                "bytes_used": per_root_used.get(key, 0),
                "max_gb": r["max_gb"],
                "max_bytes": r["max_bytes"],
            })
            for k in ("total", "free", "used"): agg[k] += d[k]
        rc = self._rec_cfg()
        # Legacy "max_bytes" retained for older UI callers = sum of
        # per-disk quotas (0 if all unlimited).
        total_max = sum(r["max_bytes"] for r in roots_q)
        result = {
            "root": str(self.primary_root()),           # legacy scalar
            "roots": roots_stats,                        # per-root, with quota+usage
            "bytes_used": int(total_bytes),
            "segment_count": int(total_count),
            "max_bytes": total_max,                      # legacy aggregate
            "retention_days": int(rc.get("retention_days", 14)),
            "disk": agg,                                 # aggregate across roots
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
        rc = self._rec_cfg()
        removed = 0; freed = 0
        retention = int(rc.get("retention_days", 14))
        now_ts = time.time()

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
            nonlocal removed, freed
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
                if self.delete_segment(sid):
                    removed += 1; freed += int(b or 0)

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

        # Per-disk quota: EACH configured storage root carries its own
        # max_gb, taken from its storage_paths entry. max_bytes=0 for a
        # root means unlimited for that disk. Segments whose path
        # doesn't match any current root land in an "other" bucket that
        # is left alone here (they'd be picked up by rescan or delete).
        roots_q = self.roots_with_quota()
        capped = [r for r in roots_q if r["max_bytes"] > 0]
        for r in capped:
            # Scoped to this root via an indexed LIKE prefix scan instead of
            # fetching every segment on every configured root and bucketing
            # in Python — this used to run a full table scan per purge tick
            # regardless of how many roots were actually over quota.
            pattern = self._like_escape(r["path"] + os.sep) + "%"
            # Over-quota must be judged against the TRUE total (locked +
            # unlocked) — the same figure pick_write_root()/health() use
            # via _bytes_used_under() — not just what's eligible to
            # delete. A root sitting on a lot of locked (protected)
            # footage still needs to be recognised as over its cap even
            # though none of that locked data can be freed here; getting
            # this backwards (summing only locked=0 rows) meant a root
            # could stay silently over quota forever whenever enough of
            # its usage happened to be locked.
            over = self._bytes_used_under(r["path"]) - r["max_bytes"]
            if over <= 0: continue
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, bytes FROM segments"
                    " WHERE locked = 0 AND path LIKE ? ESCAPE '\\'"
                    " ORDER BY started_at ASC",
                    (pattern,)
                ).fetchall()
            for sid, b in rows:
                if over <= 0: break
                b = int(b or 0)
                if self.delete_segment(sid):
                    over -= b; freed += b; removed += 1

        # Emergency global purge: when NO writable root has room, delete
        # globally-oldest UNLOCKED segments (regardless of which disk
        # they're on) until at least one root becomes writable again.
        # This is the "diskler dolduğunda eski kayıtlar silinsin" path.
        # The batch loop rechecks after each small batch so we stop as
        # soon as room appears — no gratuitous deletion.
        #
        # "Room" here must mean the same thing pick_write_root() means by
        # it — writable AND enough free space AND under quota — not just
        # "has free bytes". A root with plenty of free space but a broken
        # permission (e.g. wrong owner after a remount) is NOT room: it
        # can have gigabytes free and 0% quota used while every write to
        # it fails, which used to make this function report "someone has
        # room" and skip the emergency purge entirely — while the actual
        # writable root quietly ran itself down to ENOSPC.
        def _any_has_room() -> bool:
            for entry in roots_q:
                r = Path(entry["path"])
                try:
                    probe = r / ".rtcview_write_test"
                    probe.write_text("ok"); probe.unlink()
                    du = shutil.disk_usage(entry["path"])
                except Exception:
                    continue
                if du.free < SAFETY_MARGIN_BYTES:
                    continue
                if entry["max_bytes"] > 0:
                    if self._bytes_used_under(entry["path"]) >= entry["max_bytes"]:
                        continue
                return True
            return False

        if roots_q and not _any_has_room():
            BATCH = 5
            log.warning("emergency purge: no writable root has room")
            while not self._stop.is_set():
                with self._lock:
                    batch = self._db.execute(
                        "SELECT id, bytes FROM segments"
                        " WHERE locked = 0 ORDER BY started_at ASC LIMIT ?",
                        (BATCH,)
                    ).fetchall()
                if not batch:
                    log.error("emergency purge: nothing left to delete — all segments locked")
                    break
                for sid, b in batch:
                    if self.delete_segment(sid):
                        removed += 1; freed += int(b or 0)
                if _any_has_room():
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
        # (_ensure_roots only does that for a brand-new database — an
        # existing one keeps whatever mode it already had, see there for
        # why). Harmless to always call: SQLite documents incremental_
        # vacuum as a no-op when auto_vacuum isn't enabled. A small batch
        # every purge tick reclaims space from all the deletes above
        # without the latency spike a full VACUUM would cause.
        try:
            with self._lock:
                self._db.execute("PRAGMA incremental_vacuum(200)")
        except Exception as e:
            log.debug("incremental_vacuum failed: %s", e)

        if removed:
            log.info("purged %d segments (%.2f MB freed)", removed, freed / 1e6)
        return {"removed": removed, "freed_bytes": freed}

    # ---------- Rescan (index rebuild from disk) ----------
    def rescan(self) -> dict:
        """Walk every configured root and register any MP4 not already
        in the index, then drop any indexed row whose file no longer
        exists on disk (deleted outside the app — manual rm, an external
        script, a swapped disk). This is the manual "silinen dosyaları
        DB'den temizle" action from Ayarlar; nothing else currently
        reconciles a file removed that way."""
        from app.recorder import _parse_fname_ts   # local import to avoid cycle
        snap_root = self.snapshots_root().resolve()
        found = 0; added = 0
        for root in self.roots():
            root = root.resolve()
            for p in root.rglob("*.mp4"):
                if not p.is_file(): continue
                try:
                    if snap_root in p.parents: continue
                except Exception:
                    pass
                found += 1
                abs_p = str(p.resolve())
                with self._lock:
                    exists = self._db.execute("SELECT 1 FROM segments WHERE path = ?", (abs_p,)).fetchone()
                if exists: continue
                try:
                    rel = p.relative_to(root)
                    cam_id = rel.parts[0] if rel.parts else "unknown"
                except ValueError:
                    cam_id = "unknown"
                try:
                    st = p.stat()
                except OSError:
                    continue
                fts = _parse_fname_ts(p)
                started = fts if fts is not None else max(0.0, st.st_mtime - 1)
                self.register_segment(cam_id, abs_p, started, st.st_mtime, trigger="rescan")
                added += 1

        with self._lock:
            all_paths = self._db.execute("SELECT id, path FROM segments").fetchall()
        orphaned = [sid for sid, p in all_paths if not os.path.exists(p)]
        if orphaned:
            with self._lock:
                self._db.executemany("DELETE FROM segments WHERE id = ?",
                                     [(sid,) for sid in orphaned])
            self._invalidate_caches()
            log.info("rescan: dropped %d segment row(s) whose file no longer exists",
                     len(orphaned))
        return {"scanned": found, "added": added, "removed": len(orphaned)}


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
