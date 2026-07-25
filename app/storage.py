"""Recording storage: SQLite segment index + retention/quota purger."""
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("storage")

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
    trigger      TEXT    NOT NULL DEFAULT 'schedule'
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
"""


class Storage:
    """
    Thread-safe SQLite index for recording segments and snapshots plus a
    background purger enforcing retention (days) and quota (bytes).

    All paths are stored absolute. The storage root can change at runtime
    (see `set_root`) — new segments go under the new root; the index still
    resolves old segments by their stored absolute path.
    """

    def __init__(self, config_store):
        self.config_store = config_store
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        self._db_path: Optional[Path] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._purge_cb = None  # optional: called with cam_id when a lock is released
        self._ensure_root()

    # ---------- Root / DB ----------
    def _rec_cfg(self):
        return self.config_store.get_recording()

    def root(self) -> Path:
        return Path(self._rec_cfg()["storage_path"]).expanduser().resolve()

    def snapshots_root(self) -> Path:
        return self.root() / "_snapshots"

    def _ensure_root(self):
        try:
            root = self.root()
            root.mkdir(parents=True, exist_ok=True)
            self.snapshots_root().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("Cannot create storage root: %s", e)
            return
        db_path = root / "index.sqlite"
        with self._lock:
            if self._db_path == db_path and self._db is not None:
                return
            if self._db is not None:
                try: self._db.close()
                except Exception: pass
            self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db_path = db_path
            log.info("Storage index open: %s", db_path)

    def set_root(self, new_path: str) -> tuple[bool, str]:
        """Change storage_path. Returns (ok, message). Writability is validated."""
        p = Path(new_path).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"Klasör oluşturulamadı: {e}"
        test = p / ".rtcview_write_test"
        try:
            test.write_text("ok"); test.unlink()
        except Exception as e:
            return False, f"Klasöre yazılamıyor (izin?): {e}"
        self.config_store.update_recording({"storage_path": str(p.resolve())})
        self._ensure_root()
        return True, str(p.resolve())

    # ---------- Segments ----------
    def register_segment(self, cam_id: str, path: str, started_at: float,
                         ended_at: float, trigger: str = "schedule") -> Optional[int]:
        try:
            size = os.path.getsize(path)
        except OSError:
            log.warning("register_segment: file gone before index: %s", path)
            return None
        duration = max(0.0, ended_at - started_at)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR REPLACE INTO segments"
                " (cam_id, path, started_at, ended_at, duration, bytes, trigger)"
                " VALUES (?,?,?,?,?,?,?)",
                (cam_id, str(Path(path).resolve()), started_at, ended_at, duration, size, trigger),
            )
            return cur.lastrowid

    def list_segments(self, cam_id: Optional[str] = None,
                      t_from: Optional[float] = None,
                      t_to: Optional[float] = None,
                      limit: int = 2000) -> list[dict]:
        q = "SELECT id, cam_id, path, started_at, ended_at, duration, bytes, locked, trigger FROM segments"
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
        cols = ["id","cam_id","path","started_at","ended_at","duration","bytes","locked","trigger"]
        return [dict(zip(cols, r)) for r in rows]

    def get_segment(self, seg_id: int) -> Optional[dict]:
        with self._lock:
            r = self._db.execute(
                "SELECT id, cam_id, path, started_at, ended_at, duration, bytes, locked, trigger"
                " FROM segments WHERE id = ?", (seg_id,)
            ).fetchone()
        if not r: return None
        cols = ["id","cam_id","path","started_at","ended_at","duration","bytes","locked","trigger"]
        return dict(zip(cols, r))

    def delete_segment(self, seg_id: int, force: bool = False) -> bool:
        seg = self.get_segment(seg_id)
        if not seg: return False
        if seg["locked"] and not force: return False
        try:
            Path(seg["path"]).unlink(missing_ok=True)
        except Exception as e:
            log.warning("delete_segment: unlink failed %s: %s", seg["path"], e)
        with self._lock:
            self._db.execute("DELETE FROM segments WHERE id = ?", (seg_id,))
        _try_prune_empty(Path(seg["path"]).parent, self.root())
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

    # ---------- Stats ----------
    def stats(self) -> dict:
        with self._lock:
            total_bytes, total_count = self._db.execute(
                "SELECT COALESCE(SUM(bytes),0), COUNT(*) FROM segments"
            ).fetchone()
            per_cam = self._db.execute(
                "SELECT cam_id, COALESCE(SUM(bytes),0), COUNT(*),"
                " MIN(started_at), MAX(ended_at)"
                " FROM segments GROUP BY cam_id"
            ).fetchall()
        try:
            du = shutil.disk_usage(str(self.root()))
            disk = {"total": du.total, "free": du.free, "used": du.used}
        except Exception:
            disk = {"total": 0, "free": 0, "used": 0}
        rc = self._rec_cfg()
        return {
            "root": str(self.root()),
            "bytes_used": int(total_bytes),
            "segment_count": int(total_count),
            "max_bytes": int(rc.get("max_gb", 100)) * (1024**3),
            "retention_days": int(rc.get("retention_days", 14)),
            "disk": disk,
            "per_camera": [
                {"cam_id": c, "bytes": int(b), "count": int(n),
                 "first_at": s, "last_at": e}
                for (c, b, n, s, e) in per_cam
            ],
        }

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
        if retention > 0:
            cutoff = time.time() - retention * 86400
            with self._lock:
                old = self._db.execute(
                    "SELECT id, path, bytes FROM segments WHERE ended_at < ? AND locked = 0",
                    (cutoff,)
                ).fetchall()
            for sid, p, b in old:
                if self.delete_segment(sid):
                    removed += 1; freed += int(b or 0)

        max_bytes = int(rc.get("max_gb", 100)) * (1024**3)
        if max_bytes > 0:
            with self._lock:
                total = self._db.execute("SELECT COALESCE(SUM(bytes),0) FROM segments").fetchone()[0] or 0
            if total > max_bytes:
                with self._lock:
                    rows = self._db.execute(
                        "SELECT id, path, bytes FROM segments WHERE locked = 0 ORDER BY started_at ASC"
                    ).fetchall()
                for sid, p, b in rows:
                    if total <= max_bytes: break
                    if self.delete_segment(sid):
                        total -= int(b or 0); freed += int(b or 0); removed += 1

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

        if removed:
            log.info("purged %d segments (%.2f MB freed)", removed, freed / 1e6)
        return {"removed": removed, "freed_bytes": freed}

    # ---------- Refresh durations from real MP4 files (fixes bad DB rows) ----------
    def refresh_durations(self, ffmpeg_path: str = "ffmpeg", limit: int = 5000) -> dict:
        """Re-derive ended_at / duration for existing rows by probing the file.

        Uses ``ffprobe`` when present, falls back to reading the MP4 mvhd atom.
        Fixes rows recorded before per-segment start-time tracking landed
        (they had duration≈1 s because Linux st_ctime tracks writes).
        """
        import shutil as _sh, subprocess as _sp
        probe = _sh.which("ffprobe") or (
            _sh.which("ffprobe", path=str(Path(ffmpeg_path).parent)) if Path(ffmpeg_path).parent.as_posix() else None
        )
        with self._lock:
            rows = self._db.execute(
                "SELECT id, path, started_at, duration FROM segments ORDER BY id ASC LIMIT ?", (limit,)
            ).fetchall()
        fixed = 0; skipped = 0; missing = 0
        for sid, path, started_at, duration in rows:
            if not Path(path).exists():
                missing += 1
                continue
            real = None
            if probe:
                try:
                    out = _sp.check_output(
                        [probe, "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=nw=1:nk=1", path],
                        timeout=10, stderr=_sp.DEVNULL,
                    )
                    real = float(out.strip())
                except Exception:
                    real = None
            if real is None:
                real = _mp4_duration_from_mvhd(path)
            if real is None or real <= 0:
                skipped += 1
                continue
            # Keep started_at; recompute ended_at from real duration.
            new_end = float(started_at) + real
            with self._lock:
                self._db.execute(
                    "UPDATE segments SET duration = ?, ended_at = ? WHERE id = ?",
                    (real, new_end, sid),
                )
            fixed += 1
        return {"fixed": fixed, "skipped": skipped, "missing": missing, "total": len(rows)}

    # ---------- Rescan (index rebuild from disk) ----------
    def rescan(self) -> dict:
        """Walk storage root and register any MP4 not already in the index."""
        root = self.root()
        found = 0; added = 0
        for p in root.rglob("*.mp4"):
            if not p.is_file(): continue
            found += 1
            abs_p = str(p.resolve())
            with self._lock:
                exists = self._db.execute("SELECT 1 FROM segments WHERE path = ?", (abs_p,)).fetchone()
            if exists: continue
            try:
                cam_id = p.parent.parent.parent.parent.name  # <root>/<cam>/YYYY/MM/DD/file
            except Exception:
                cam_id = "unknown"
            try:
                st = p.stat()
            except OSError:
                continue
            self.register_segment(cam_id, abs_p, st.st_mtime - 1, st.st_mtime, trigger="rescan")
            added += 1
        return {"scanned": found, "added": added}


def _mp4_duration_from_mvhd(path: str) -> Optional[float]:
    """Parse an MP4's mvhd box to read duration in seconds.

    Faststart files put the moov early, but for -movflags +faststart or a
    non-faststart file we may need to walk boxes. Keep it small: scan up to
    16 MB, follow moov > mvhd. Returns None on any parse error.
    """
    import struct
    try:
        with open(path, "rb") as f:
            data = f.read(16 * 1024 * 1024)
    except OSError:
        return None
    pos = 0
    def _boxes(buf, start, end):
        i = start
        while i + 8 <= end:
            size = struct.unpack(">I", buf[i:i+4])[0]
            btype = buf[i+4:i+8]
            hdr = 8
            if size == 1:
                if i + 16 > end: return
                size = struct.unpack(">Q", buf[i+8:i+16])[0]
                hdr = 16
            if size < hdr or i + size > end + 1:
                return
            yield btype, i + hdr, i + size
            i += size
    for btype, s, e in _boxes(data, 0, len(data)):
        if btype == b"moov":
            for bt2, s2, e2 in _boxes(data, s, e):
                if bt2 == b"mvhd":
                    if e2 - s2 < 32: return None
                    version = data[s2]
                    if version == 1:
                        if e2 - s2 < 32: return None
                        timescale = struct.unpack(">I", data[s2+20:s2+24])[0]
                        duration  = struct.unpack(">Q", data[s2+24:s2+32])[0]
                    else:
                        timescale = struct.unpack(">I", data[s2+12:s2+16])[0]
                        duration  = struct.unpack(">I", data[s2+16:s2+20])[0]
                    if timescale == 0: return None
                    return float(duration) / float(timescale)
    return None


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
