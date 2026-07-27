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
        rc = self._rec_cfg()
        raw = list(rc.get("storage_paths") or [])
        if not raw and rc.get("storage_path"):
            raw = [rc["storage_path"]]
        out = []
        seen = set()
        for r in raw:
            if not r: continue
            p = Path(str(r)).expanduser().resolve()
            if p in seen: continue
            seen.add(p); out.append(p)
        return out

    def primary_root(self) -> Path:
        rs = self.roots()
        return rs[0] if rs else Path("/opt/rtcview/recordings").resolve()

    # Kept for legacy call-sites (rescan / etc.). Returns the primary.
    def root(self) -> Path:
        return self.primary_root()

    def snapshots_root(self) -> Path:
        return self.primary_root() / "_snapshots"

    def pick_write_root(self) -> Path:
        """Choose a root for a new recording session.

        Strategy: whichever CONFIGURED, USABLE root has the most free
        bytes right now. That naturally spreads load, and also fails
        over — a root that has become unwritable is skipped, and a root
        that has become full loses priority to a healthier peer.
        """
        best = None; best_free = -1
        for r in self.roots():
            try:
                r.mkdir(parents=True, exist_ok=True)
                # Cheap writability probe (fs write cache; not fsync)
                probe = r / ".rtcview_write_test"
                probe.write_text("ok"); probe.unlink()
                du = shutil.disk_usage(str(r))
                if du.free > best_free:
                    best_free = du.free; best = r
            except Exception:
                continue
        if best is None:
            # Nothing writable — return primary anyway; the recorder
            # will fail cleanly and the health endpoint will surface it.
            return self.primary_root()
        return best

    def _ensure_roots(self):
        try:
            for r in self.roots():
                r.mkdir(parents=True, exist_ok=True)
            self.snapshots_root().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("Cannot create storage root: %s", e)
            return
        db_path = self.primary_root() / "index.sqlite"
        with self._lock:
            if self._db_path == db_path and self._db is not None:
                return
            if self._db is not None:
                try: self._db.close()
                except Exception: pass
            self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
            self._db.executescript(SCHEMA)
            # PRAGMA tuning — WAL for concurrency, mmap for cheap reads,
            # 4 MB page cache (default 2 MB), temp tables in RAM.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA cache_size=-4000")
            self._db.execute("PRAGMA mmap_size=67108864")
            self._db.execute("PRAGMA temp_store=MEMORY")
            self._db_path = db_path
            log.info("Storage index open: %s (primary root)", db_path)

    def set_roots(self, new_paths: list[str]) -> tuple[bool, str]:
        """Replace the entire storage_paths list. Every entry must be
        creatable + writable. Returns (ok, message)."""
        if not new_paths:
            return False, "En az bir kayıt klasörü olmalı"
        clean: list[str] = []
        seen: set = set()
        for np in new_paths:
            s = str(np or "").strip()
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
            seen.add(rp); clean.append(rp)
        if not clean:
            return False, "Geçerli klasör bulunamadı"
        self.config_store.update_recording({"storage_paths": clean, "storage_path": clean[0]})
        self._ensure_roots()
        self._invalidate_caches()
        return True, ", ".join(clean)

    def set_root(self, new_path: str) -> tuple[bool, str]:
        """Legacy single-path API used by older /api/recording/settings
        callers. Just wraps set_roots with a single-element list."""
        return self.set_roots([new_path])

    def _invalidate_caches(self):
        self._stats_cache = None
        self._health_cache = None
        self._disk_cache.clear()

    # ---------- Segments ----------
    def register_segment(self, cam_id: str, path: str, started_at: float,
                         ended_at: float, trigger: str = "schedule") -> Optional[int]:
        try:
            size = os.path.getsize(path)
        except OSError:
            log.warning("register_segment: file gone before index: %s", path)
            return None
        duration = max(0.0, ended_at - started_at)
        abs_path = str(Path(path).resolve())
        self._stats_cache = None   # invalidate — segment count/bytes changed
        with self._lock:
            # INSERT OR IGNORE preserves the row (and its id + locked flag) if
            # a segment is re-registered; then an UPDATE tops up the ended_at
            # / bytes / trigger values. INSERT OR REPLACE would delete-then-
            # insert with a new id, breaking any URLs the client already holds.
            cur = self._db.execute(
                "INSERT OR IGNORE INTO segments"
                " (cam_id, path, started_at, ended_at, duration, bytes, trigger)"
                " VALUES (?,?,?,?,?,?,?)",
                (cam_id, abs_path, started_at, ended_at, duration, size, trigger),
            )
            if cur.rowcount == 0:
                self._db.execute(
                    "UPDATE segments SET ended_at=?, duration=?, bytes=?, trigger=?"
                    " WHERE path=?",
                    (ended_at, duration, size, trigger, abs_path),
                )
                r = self._db.execute("SELECT id FROM segments WHERE path=?", (abs_path,)).fetchone()
                return r[0] if r else None
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
        self._stats_cache = None
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
        for r in self.roots():
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
                except Exception as e:
                    r_errs.append(f"Yazılamıyor: {e}")
                try:
                    du = shutil.disk_usage(str(r))
                    disk = {"total": du.total, "free": du.free, "used": du.used}
                    free_pct = (du.free / du.total) * 100 if du.total else 0
                    if du.free < 1 * 1024**3:
                        r_errs.append(f"Disk neredeyse dolu: {du.free // (1024*1024)} MB")
                    elif free_pct < 10:
                        r_warns.append(f"%{100 - free_pct:.0f} dolu")
                except Exception as e:
                    r_errs.append(f"Disk okunamadı: {e}")
            rst = "error" if r_errs else ("warning" if r_warns else "ok")
            per_root.append({
                "path": str(r), "status": rst, "exists": exists, "writable": writable,
                "disk": disk, "free_percent": round(free_pct, 1),
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

        # disk usage per configured root, cached individually (5 s)
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
            roots_stats.append({"path": key, "disk": d})
            for k in ("total", "free", "used"): agg[k] += d[k]
        rc = self._rec_cfg()
        result = {
            "root": str(self.primary_root()),           # legacy scalar
            "roots": roots_stats,                        # per-root disk stats
            "bytes_used": int(total_bytes),
            "segment_count": int(total_count),
            "max_bytes": int(rc.get("max_gb", 100)) * (1024**3),
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

        # Per-disk quota: max_gb applies to EACH configured storage root
        # independently, not to the sum across roots. This means adding a
        # second 500 GB disk with max_gb=100 gets you 100+100 GB of
        # recordings, not still 100 total. Segments whose path doesn't
        # match any configured root (e.g. legacy files from an old path)
        # fall into an "other" bucket that still shares the same cap.
        max_bytes = int(rc.get("max_gb", 100)) * (1024**3)
        if max_bytes > 0:
            root_strs = [str(r) + os.sep for r in self.roots()]
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, path, bytes FROM segments WHERE locked = 0 ORDER BY started_at ASC"
                ).fetchall()
            # Group + sum per root (oldest-first order preserved for delete).
            by_root: dict[str, list[tuple[int, str, int]]] = {}
            totals: dict[str, int] = {}
            for sid, p, b in rows:
                key = next((rs for rs in root_strs if p.startswith(rs)), "__other__")
                by_root.setdefault(key, []).append((sid, p, int(b or 0)))
                totals[key] = totals.get(key, 0) + int(b or 0)
            for key, seglist in by_root.items():
                over = totals[key] - max_bytes
                if over <= 0: continue
                for sid, p, b in seglist:
                    if over <= 0: break
                    if self.delete_segment(sid):
                        over -= b; freed += b; removed += 1

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

    # ---------- Rescan (index rebuild from disk) ----------
    def rescan(self) -> dict:
        """Walk every configured root and register any MP4 not already
        in the index."""
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
        return {"scanned": found, "added": added}


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
