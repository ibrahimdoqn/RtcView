"""FFmpeg-based per-camera segment recorder.

Design
------
* Each camera with an eligible mode gets its own FFmpeg subprocess that reads
  from go2rtc's RTSP output (``rtsp://<host>:<rtsp_port>/<stream>``) and writes
  numbered segments to disk (``-f segment -segment_time N -reset_timestamps 1``).
* A watcher thread per camera scans that camera's day directory for freshly
  closed segments and registers them into the SQLite index.
* A supervisor thread evaluates each camera every few seconds — starts/stops
  processes according to ``record_mode`` (off/always/schedule/manual) and the
  weekly ``record_schedule``, and revives processes that died.
"""
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("recorder")

MANUAL_DEFAULT_SECONDS = 600  # 10 minutes
SUPERVISOR_INTERVAL = 3
WATCHER_INTERVAL = 2


def _cam_dir(root: Path, cam_id: str) -> Path:
    now = datetime.now()
    return root / cam_id / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"


# Segment filenames are strftime-templated by ffmpeg so each file's name
# encodes the exact wall-clock instant it was opened. This lets scan_and_
# register recover accurate started_at values from the filename alone —
# no memory dicts, no watcher-polling drift.
_FNAME_RE = re.compile(r"_(\d{8})_(\d{6})\.[A-Za-z0-9]+$")


def _parse_fname_ts(path: Path) -> Optional[float]:
    m = _FNAME_RE.search(path.name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.timestamp()
    except ValueError:
        return None


def _is_root_usable(r: Path) -> bool:
    try:
        r.mkdir(parents=True, exist_ok=True)
        probe = r / ".rtcview_write_test"
        probe.write_text("ok"); probe.unlink()
        return True
    except Exception:
        return False


def _schedule_active(schedule: list, now: Optional[datetime] = None) -> bool:
    """Return True if a schedule window covers ``now``.

    Each entry: ``{"days":[0..6 mon=0], "start":"HH:MM", "end":"HH:MM"}``.
    An empty ``days`` list means every day. ``end`` <= ``start`` wraps midnight.
    """
    if not schedule: return False
    now = now or datetime.now()
    dow = now.weekday()
    hm = now.hour * 60 + now.minute
    for w in schedule:
        days = w.get("days") or []
        if days and dow not in days: continue
        try:
            sh, sm = map(int, str(w.get("start","00:00")).split(":"))
            eh, em = map(int, str(w.get("end","23:59")).split(":"))
        except Exception:
            continue
        s = sh*60 + sm; e = eh*60 + em
        if e > s:
            if s <= hm < e: return True
        else:  # wraps midnight
            if hm >= s or hm < e: return True
    return False


class CameraRecorder:
    """State + subprocess for a single camera."""

    def __init__(self, cam: dict, ffmpeg_path: str, segment_seconds: int,
                 rtsp_url: str, storage, container: str = "mp4"):
        self.cam = cam
        self.cam_id = cam["id"]
        self.ffmpeg_path = ffmpeg_path
        self.segment_seconds = max(30, int(segment_seconds))
        self.rtsp_url = rtsp_url
        self.storage = storage
        self.container = container
        self.proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.day_dir: Optional[Path] = None
        self.pattern: Optional[str] = None
        self.ext: str = "mp4"
        # Chosen fresh at each start() call from storage.pick_write_root().
        # Multi-root aware: different sessions of the same camera may end
        # up on different disks depending on free-space at spawn time.
        self.root: Optional[Path] = None
        self.manual_until: float = 0.0  # unix ts; > now means manual override on
        self.trigger: str = "schedule"
        # Bounded deque is thread-safe for append/pop-left; the stderr
        # drainer thread and the status endpoint no longer race on a plain
        # list.
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._stderr_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, trigger: str):
        with self._lock:
            if self.is_running(): return
            # Pick the freshest, most-free storage root at spawn time.
            # A previous session may have used a different root — we
            # honour whatever has room now.
            self.root = self.storage.pick_write_root()
            self.day_dir = _cam_dir(self.root, self.cam_id)
            self.day_dir.mkdir(parents=True, exist_ok=True)
            self.ext = "mp4" if self.container == "mp4" else "mkv"
            # Filename encodes each segment's own wall-clock start time via
            # ffmpeg's -strftime 1. Second resolution is unique because
            # segment_time >= 30 s.
            self.pattern = str(self.day_dir / f"{self.cam_id}_%Y%m%d_%H%M%S.{self.ext}")
            audio = bool(self.cam.get("record_audio", False))
            cmd = [
                self.ffmpeg_path,
                "-hide_banner", "-loglevel", "warning", "-nostdin",
                # Shorter probe: RTSP is a known-good source, we don't need
                # ffmpeg's default 5 s codec-sniffing before starting.
                "-analyzeduration", "500000", "-probesize", "500000",
                "-fflags", "+genpts",
                "-rtsp_transport", "tcp",
                "-i", self.rtsp_url,
                "-map", "0:v:0",
            ]
            if audio:
                cmd += ["-map", "0:a:0?"]
            # Video is always stream-copied (H.264/H.265 from go2rtc → MP4).
            # Audio, when requested, is transcoded to AAC so MP4 muxing works
            # regardless of what the camera sends (pcm_alaw / pcm_mulaw / mp2
            # / opus are all common but MP4-incompatible with -c copy).
            cmd += ["-c:v", "copy"]
            if audio:
                cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]
            cmd += [
                "-f", "segment",
                "-segment_time", str(self.segment_seconds),
                "-segment_format", "mp4" if self.ext == "mp4" else "matroska",
                "-reset_timestamps", "1",
                "-strftime", "1",
                "-movflags", "+faststart",
                self.pattern,
            ]
            log.info("[%s] ffmpeg start: %s", self.cam_id, " ".join(shlex.quote(c) for c in cmd))
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    close_fds=True, start_new_session=True,
                )
            except FileNotFoundError:
                log.error("[%s] ffmpeg not found at %s", self.cam_id, self.ffmpeg_path)
                self.proc = None
                return
            self.started_at = time.time()
            self.trigger = trigger
            self._stderr_tail.clear()
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True, name=f"rec-err-{self.cam_id}")
            self._stderr_thread.start()

    def _drain_stderr(self):
        p = self.proc
        if not p or not p.stderr: return
        try:
            for raw in iter(p.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line: continue
                self._stderr_tail.append(line)
        except Exception:
            pass

    def stop(self):
        with self._lock:
            p = self.proc
            if p and p.poll() is None:
                log.info("[%s] ffmpeg stop", self.cam_id)
                # Graceful shutdown so ffmpeg has a chance to finalise the
                # currently-open MP4 (write the moov atom, close file).
                # Escalate through SIGTERM → SIGINT → SIGKILL.
                try: p.terminate()
                except Exception: pass
                try:
                    p.wait(timeout=4)
                except Exception:
                    try:
                        import signal
                        p.send_signal(signal.SIGINT)
                        p.wait(timeout=2)
                    except Exception:
                        try: p.kill(); p.wait(timeout=1)
                        except Exception: pass
            self.proc = None
            # Register the final segment BEFORE forgetting started_at:
            # _scan_and_register's very first line is a guard on that field,
            # so calling it after clearing started_at made every stop/reload
            # silently drop the in-progress segment — never indexed, invisible
            # in playback and in retention/quota accounting, until a manual
            # rescan. The process is already confirmed dead at this point
            # (terminate/wait above), so the file's final flush is on disk.
            self._scan_and_register(final=True)
            self.started_at = None

    def _scan_and_register(self, final: bool = False):
        """Register newly-closed segments.

        Filenames encode each segment's own wall-clock start time (ffmpeg
        ``-strftime 1``). ``started_at`` is parsed from the filename; the
        end of a closed file equals the start of the next file (accurate
        to the second — no watcher-polling drift). Only files that were
        opened by THIS recorder instance are considered, so leftover files
        from a previous crashed process are never double-registered.
        """
        if not self.day_dir or not self.started_at:
            return
        # Only pick up files whose parsed start is >= this session's spawn
        # time — everything else belongs to a previous run and is already
        # (or will be) recorded through its own recorder or via rescan.
        session_start = self.started_at - 5  # 5s slack for clock jitter
        try:
            candidates = sorted(self.day_dir.glob(f"{self.cam_id}_*.{self.ext}"))
        except Exception:
            return
        files: list[tuple[Path, float]] = []
        for p in candidates:
            ts = _parse_fname_ts(p)
            if ts is None or ts < session_start:
                continue
            files.append((p, ts))
        if not files:
            return

        get_by_path = getattr(self.storage, "get_segment_by_path", None)
        for i, (f, started) in enumerate(files):
            path = str(f.resolve())
            try:
                st = f.stat()
            except OSError:
                continue
            is_latest = (i == len(files) - 1)
            if not (final or not is_latest):
                # Latest file is still being written; register on rollover.
                continue
            if get_by_path and get_by_path(path):
                continue
            if not is_latest:
                ended = files[i+1][1]  # next segment's start = this one's close
            else:
                ended = st.st_mtime  # final flush
            if ended <= started:
                ended = started + max(1.0, st.st_size / 500_000.0)
            self.storage.register_segment(self.cam_id, path, started, ended, trigger=self.trigger)

    def status(self) -> dict:
        return {
            "cam_id": self.cam_id,
            "running": self.is_running(),
            "started_at": self.started_at,
            "trigger": self.trigger,
            "manual_until": self.manual_until,
            "last_error": (self._stderr_tail[-1] if self._stderr_tail else ""),
        }


class RecordingManager:
    def __init__(self, config_store, storage):
        self.cfg = config_store
        self.storage = storage
        self._recs: dict[str, CameraRecorder] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sup_thread: Optional[threading.Thread] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._reload_timer: Optional[threading.Timer] = None
        # Patch a convenience lookup onto storage without touching its module.
        if not hasattr(storage, "get_segment_by_path"):
            def _by_path(p, _s=storage):
                with _s._lock:
                    r = _s._db.execute("SELECT id FROM segments WHERE path = ?", (str(Path(p).resolve()),)).fetchone()
                return {"id": r[0]} if r else None
            storage.get_segment_by_path = _by_path  # type: ignore[attr-defined]

    # ---------- lifecycle ----------
    def start(self):
        with self._lock:
            if self._sup_thread and self._sup_thread.is_alive(): return
            self._stop.clear()
            self._sup_thread = threading.Thread(target=self._supervisor_loop, daemon=True, name="rec-supervisor")
            self._watch_thread = threading.Thread(target=self._watcher_loop, daemon=True, name="rec-watcher")
            self._sup_thread.start()
            self._watch_thread.start()
            log.info("RecordingManager started")

    def stop(self):
        self._stop.set()
        with self._lock:
            recs = list(self._recs.values())
        for r in recs:
            try: r.stop()
            except Exception as e: log.warning("stop failed for %s: %s", r.cam_id, e)
        if self._sup_thread: self._sup_thread.join(timeout=3)
        if self._watch_thread: self._watch_thread.join(timeout=3)

    def reload_camera(self, cam_id: str):
        """Called when a camera's config changed; kill and re-evaluate.

        Holds the manager lock across both the pop AND the stop, not just
        the pop. _tick() also takes this lock to read/write self._recs; if
        the pop released it before r.stop() (which can take up to ~7s to
        walk terminate -> SIGINT -> SIGKILL) finished, a tick landing in
        that window sees cam_id as absent, wants it running, and starts a
        brand-new CameraRecorder while the old ffmpeg process is still
        alive — two processes writing the exact same second-resolution
        filename. A few seconds of the supervisor waiting for this lock is
        a fair trade for that never happening.
        """
        with self._lock:
            r = self._recs.pop(cam_id, None)
            if r:
                try: r.stop()
                except Exception: pass

    def reload_all(self):
        """Debounced full reload. Called from several settings endpoints
        that can fire in quick succession — the debounce collapses those
        into one recorder restart cycle instead of thrashing ffmpeg."""
        with self._lock:
            if self._reload_timer:
                self._reload_timer.cancel()
            self._reload_timer = threading.Timer(0.4, self._do_reload_all)
            self._reload_timer.daemon = True
            self._reload_timer.start()

    def _do_reload_all(self):
        with self._lock:
            ids = list(self._recs.keys())
            self._reload_timer = None
        for cid in ids:
            self.reload_camera(cid)

    # ---------- external API ----------
    def manual_start(self, cam_id: str, seconds: int = MANUAL_DEFAULT_SECONDS) -> bool:
        cam = self._find_cam(cam_id)
        if not cam: return False
        # One atomic critical section covers get-or-create so two racing
        # POSTs never both build a recorder and orphan one of them.
        with self._lock:
            r = self._recs.get(cam_id)
            if not r:
                r = self._build(cam)
                if not r: return False
                self._recs[cam_id] = r
            r.manual_until = time.time() + max(10, int(seconds))
            r.trigger = "manual"
        if not r.is_running(): r.start(trigger="manual")
        return True

    def manual_stop(self, cam_id: str) -> bool:
        with self._lock:
            r = self._recs.get(cam_id)
        if not r: return False
        r.manual_until = 0.0
        # Supervisor will stop it on the next tick if no other mode wants it running.
        return True

    def status(self) -> list[dict]:
        with self._lock:
            recs = list(self._recs.values())
        out = [r.status() for r in recs]
        for c in self.cfg.get_cameras():
            if not any(x["cam_id"] == c["id"] for x in out):
                out.append({"cam_id": c["id"], "running": False, "started_at": None,
                            "trigger": None, "manual_until": 0, "last_error": ""})
        return out

    # ---------- internals ----------
    def _find_cam(self, cam_id: str) -> Optional[dict]:
        for c in self.cfg.get_cameras():
            if c["id"] == cam_id: return c
        return None

    def _rtsp_url(self, cam: dict) -> str:
        gc = self.cfg.get_go2rtc()
        host = gc.get("host", "127.0.0.1")
        port = int(gc.get("rtsp_port", 8554))
        stream = cam.get("stream") or cam["id"]
        return f"rtsp://{host}:{port}/{stream}"

    def _build(self, cam: dict) -> Optional[CameraRecorder]:
        rc = self.cfg.get_recording()
        if not rc.get("enabled", True): return None
        ffmpeg = rc.get("ffmpeg_path") or "ffmpeg"
        if not shutil.which(ffmpeg) and not os.path.isfile(ffmpeg):
            log.warning("ffmpeg not available at %r — recording disabled for %s", ffmpeg, cam["id"])
            return None
        # Multi-root: at least ONE configured root must be usable. The
        # recorder itself picks a specific root at start() time from
        # storage.pick_write_root() so a full/failed root is skipped.
        if not any(_is_root_usable(r) for r in self.storage.roots()):
            log.warning("no usable storage root — skipping recorder for %s", cam["id"])
            return None
        return CameraRecorder(
            cam=cam, ffmpeg_path=ffmpeg,
            segment_seconds=int(rc.get("segment_seconds", 300)),
            rtsp_url=self._rtsp_url(cam), storage=self.storage,
        )

    def _wants_run(self, cam: dict, now: float) -> tuple[bool, str]:
        mode = cam.get("record_mode", "off")
        if mode == "off": return False, ""
        if mode == "always": return True, "always"
        if mode == "schedule":
            return (_schedule_active(cam.get("record_schedule") or []), "schedule")
        if mode == "manual":
            # Manual mode only runs when explicitly triggered via manual_start.
            r = self._recs.get(cam["id"])
            if r and r.manual_until > now: return True, "manual"
            return False, ""
        return False, ""

    def _supervisor_loop(self):
        while not self._stop.wait(SUPERVISOR_INTERVAL):
            try:
                self._tick()
            except Exception as e:
                log.exception("supervisor tick failed: %s", e)

    def _tick(self):
        rc = self.cfg.get_recording()
        recording_globally_on = rc.get("enabled", True)
        now = time.time()
        cams = self.cfg.get_cameras()
        with self._lock:
            active_ids = set(self._recs.keys())
            cam_ids = {c["id"] for c in cams}
            # Drop recorders for deleted cameras.
            for gone in active_ids - cam_ids:
                r = self._recs.pop(gone, None)
                if r:
                    try: r.stop()
                    except Exception: pass
        for cam in cams:
            want, trigger = (False, "")
            # Manual override wins regardless of schedule.
            r = self._recs.get(cam["id"])
            if r and r.manual_until > now:
                want, trigger = True, "manual"
            elif recording_globally_on:
                want, trigger = self._wants_run(cam, now)
            if want:
                if r is None:
                    r = self._build(cam)
                    if not r: continue
                    with self._lock: self._recs[cam["id"]] = r
                if not r.is_running(): r.start(trigger=trigger)
                else: r.trigger = trigger
            else:
                if r and r.is_running(): r.stop()

    def _watcher_loop(self):
        while not self._stop.wait(WATCHER_INTERVAL):
            with self._lock:
                recs = list(self._recs.values())
            for r in recs:
                try:
                    if r.is_running(): r._scan_and_register(final=False)
                except Exception as e:
                    log.debug("watcher %s: %s", r.cam_id, e)
