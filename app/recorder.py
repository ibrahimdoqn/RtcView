"""FFmpeg-based per-camera segment recorder.

Design
------
* Each camera with an eligible mode gets its own FFmpeg subprocess that reads
  from go2rtc's RTSP output (``rtsp://<host>:<rtsp_port>/<stream>``) and writes
  numbered segments to disk (``-f segment -segment_time N -reset_timestamps 1``).
* A single shared watcher thread ticks every WATCHER_INTERVAL, but only
  actually scans a camera's day directory when that camera's segment is
  expected to be closing (each CameraRecorder tracks its own
  ``segment_seconds`` — whatever it's configured to, not a hardcoded
  default — and computes when its current segment should roll over).
  Outside that window the tick is a free in-memory time check, no I/O at
  all; near/at the expected close it polls every tick until the rollover
  is actually observed, so a slow/jittery ffmpeg is still caught quickly.
* A supervisor thread evaluates each camera every few seconds — starts/stops
  processes according to ``record_mode`` (off/always/schedule/manual) and the
  weekly ``record_schedule``, and revives processes that died. It also
  restarts a recorder whose ffmpeg process has grown well past its normal
  memory footprint (stream-copy ffmpeg observed in the field: tens of MB,
  stable) — a defensive net against ffmpeg-level memory growth on a
  particular camera's stream that a plain is-it-still-running check can't
  see, since the process stays "running" the whole time it's leaking.
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
WATCHER_INTERVAL = 2  # cheap in-memory tick; see _watcher_loop — real I/O only happens near a due segment close
TRACK_MARGIN_SEC = 5  # start polling a recorder's directory this many seconds before its segment is expected to close

# Memory watchdog — see module docstring. Fallback default only; the
# real value each CameraRecorder uses is per-instance (self.mem_ceiling_mb,
# set from recording.mem_rss_ceiling_mb — Ayarlar > Sistem) since the
# right ceiling depends on the host's total RAM budget, not something one
# constant fits for both an SBC and a beefy NAS. A healthy stream-copy
# ffmpeg process (this app never re-encodes video) has been observed
# sitting at 50-90 MB RSS indefinitely; 128 MB (~1.5-2.5x that) is tight
# enough to matter on a small board — a real leak (one camera's ffmpeg
# was seen at 1.4 GB after growing unbounded over ~24h) gets caught fast,
# at the cost of possibly restarting a healthy recorder during a
# legitimate transient bump (e.g. the faststart moov rewrite on a large
# segment) more often than a looser ceiling would. A restart here is
# cheap (a few seconds of reconnect), so that trade-off favors protecting
# total system RAM.
MEM_RSS_CEILING_MB = 128
# Don't restart the same camera more than once per cooldown window even if
# it keeps re-leaking quickly — avoids a thrash loop; the camera's stream
# itself likely needs attention if this keeps firing (check its network
# path/firmware), which is exactly why every trigger is logged clearly.
MEM_CHECK_COOLDOWN_SEC = 300

# Staleness watchdog — backstop for a stuck ffmpeg the -timeout flag
# didn't catch (any other blocking call, not just the RTSP socket read),
# AND for a segment that's simply running way past its configured
# duration (e.g. a stream discontinuity confusing the segment muxer's
# internal clock — see -use_wallclock_as_timestamps above). A segment
# overdue by this many multiples of segment_seconds — with a flat floor
# so a short segment_seconds doesn't make this trigger-happy — means the
# recorder is "running" but not actually producing what it should.
# STALE_CEIL_SEC additionally bounds the worst case in absolute terms: a
# large configured segment_seconds (e.g. 30 min) should still never be
# allowed to overrun by more than 10 minutes before being cut — the
# longer a glitched segment is left open, the more of it ends up
# unplayable if it never gets a valid trailer (see _mp4_has_moov).
STALE_MULTIPLIER = 2
STALE_FLOOR_SEC = 90
STALE_CEIL_SEC = 600

# How often start() is allowed to log "can't record, tmpfs unavailable" for
# the same camera — see CameraRecorder._log_stage_blocked(). The supervisor
# retries start() every SUPERVISOR_INTERVAL regardless, so this only caps
# log volume, not how quickly recording actually resumes once tmpfs is
# usable again.
STAGE_BLOCK_LOG_INTERVAL_SEC = 60

# Every segment is staged in tmpfs (RAM) while ffmpeg writes it — this is
# mandatory, not a toggle. The finished file is moved onto whichever
# storage root currently has room once closed, picked FRESH at every
# single move (see CameraRecorder._finish_segment()), never a value fixed
# back when the recorder started. This is the architectural point: ffmpeg
# itself only ever depends on tmpfs, which is unaffected by any physical
# disk being full, slow, or gone, so the live recording process never
# needs to stop or restart for a storage reason — only WHERE a closed
# segment lands can change, transparently, segment to segment. Also
# easier on flash wear (one bulk sequential write per closed segment
# instead of a steady trickle of small buffered writes for the whole
# segment_seconds duration) and more resilient to a slow/stalling disk
# corrupting an in-progress segment, since ffmpeg is never blocked on
# whatever the storage medium happens to be doing.
#
# There is deliberately NO direct-to-disk fallback: tmpfs is a hard
# requirement, not something the recorder degrades around. If /tmp isn't
# confirmed tmpfs, or doesn't have self.tmpfs_safety_margin_bytes free
# right now, CameraRecorder.start() simply refuses to start that camera's
# ffmpeg (logged, rate-limited — see STAGE_BLOCK_LOG_INTERVAL_SEC) and the
# supervisor retries every SUPERVISOR_INTERVAL until it succeeds. This is
# a deliberate trade: a board without usable tmpfs RAM plainly can't
# record rather than silently falling back to a completely different
# (disk-write) code path — one predictable, always-tmpfs behavior to
# configure and reason about, instead of two.
#
# The one genuine risk this introduces is RAM: TMPFS_STAGE_ROOT is
# typically backed by RAM on an SBC (this was built for the NanoPi R4S)
# rather than a real disk, and unlike a storage root there's no large
# pool to fall back on. CameraRecorder._enforce_stage_cap() bounds that
# risk WITHOUT ever stopping the recorder: it drops the OLDEST
# not-yet-moved segment (accepting that one loss) the moment one camera's
# own unmoved backlog exceeds self.tmpfs_hard_cap_bytes, which only
# happens if every configured storage root is genuinely out of room or
# unreachable (normal steady-state usage is at most ~one in-flight
# segment). Both thresholds are per-instance, configurable via
# recording.tmpfs_safety_margin_mb / tmpfs_hard_cap_mb — these two
# constants are only the fallback defaults (see CameraRecorder.__init__).
TMPFS_STAGE_ROOT = Path("/tmp/rtcview-stage")
TMPFS_STAGE_SAFETY_MARGIN_BYTES = 256 * 1024**2   # 256 MB
TMPFS_STAGE_HARD_CAP_BYTES = 512 * 1024**2        # 512 MB, per camera


def _is_tmpfs(path: Path) -> bool:
    """Whether `path` is actually backed by tmpfs (RAM) and not a regular
    disk-backed mount — on some boards /tmp is just part of the root
    filesystem, on the SAME disk as storage_path, in which case staging
    would still "work" mechanically (files move successfully) but
    provide none of the point of the feature (still real disk I/O, just
    via an extra copy). Gates whether CameraRecorder.start() will let
    recording start AT ALL — see its call site; there is no fallback, so
    a False here means that camera simply doesn't record until this
    later returns True. /proc/mounts
    is reliably present on any Linux system this app targets, so a
    failure to read it is treated as "not confirmed" (False) rather
    than "assume yes"."""
    try:
        best = None
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt, fstype = parts[1], parts[2]
                if str(path).startswith(mnt) and (best is None or len(mnt) > len(best[0])):
                    best = (mnt, fstype)
        return best is not None and best[1] == "tmpfs"
    except Exception:
        return False


def tmpfs_stage_status() -> dict:
    """Live info for the UI (Ayarlar > Kayıt & Depolama): whether the
    tmpfs staging path is confirmed to be real RAM, and its current
    usage — same shape/purpose as storage.stats()'s "disk" block, so the
    frontend can render an identical usage bar. Staging is a hard
    requirement (see TMPFS_STAGE_ROOT's comment) — is_tmpfs False here
    means recording is not merely degraded but fully stopped for every
    camera until it clears, so this is the first thing to check when
    nothing is recording. Checks TMPFS_STAGE_ROOT's parent (/tmp) rather
    than TMPFS_STAGE_ROOT itself since the latter is only created lazily
    once some camera
    actually activates staging — both share the same mount either way."""
    mount_path = TMPFS_STAGE_ROOT.parent
    is_tmpfs = _is_tmpfs(mount_path)
    try:
        du = shutil.disk_usage(str(mount_path))
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except Exception:
        disk = {"total": 0, "used": 0, "free": 0}
    return {"path": str(TMPFS_STAGE_ROOT), "is_tmpfs": is_tmpfs, "disk": disk}


def _proc_rss_mb(pid: int) -> Optional[float]:
    """Resident set size of a PID in MB, straight from /proc — no extra
    dependency (psutil, etc.) for a single-line read."""
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


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


def _mp4_has_moov(path: Path, max_boxes: int = 64) -> bool:
    """Return True iff the file has a top-level 'moov' box — i.e. ffmpeg
    actually finished writing a valid trailer/index for this segment. A
    segment whose ffmpeg process was interrupted mid-write (forced
    restart racing a still-open segment, or a stream glitch confusing
    the muxer's timing — see -use_wallclock_as_timestamps and the
    staleness watchdog above) can land on disk at a normal-looking size
    with only 'ftyp'/'mdat' boxes and no 'moov' — no player can open it,
    but nothing about the file's presence/size says so. Plain top-level
    ISO-BMFF box-header scan, no ffprobe subprocess needed."""
    try:
        size = path.stat().st_size
        if size < 8:
            return False
        with open(path, "rb") as f:
            pos = 0
            for _ in range(max_boxes):
                if pos + 8 > size:
                    break
                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    break
                box_size = int.from_bytes(header[0:4], "big")
                box_type = header[4:8]
                if box_type == b"moov":
                    return True
                if box_size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    box_size = int.from_bytes(ext, "big")
                    if box_size < 16:
                        break
                elif box_size == 0:
                    break  # box runs to EOF and it wasn't moov
                elif box_size < 8:
                    break  # corrupt header, give up
                pos += box_size
        return False
    except OSError:
        return True  # couldn't verify — don't falsely flag on our own I/O error


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
                 rtsp_url: str, storage,
                 mem_ceiling_mb: int = MEM_RSS_CEILING_MB,
                 tmpfs_safety_margin_mb: int = TMPFS_STAGE_SAFETY_MARGIN_BYTES // (1024*1024),
                 tmpfs_hard_cap_mb: int = TMPFS_STAGE_HARD_CAP_BYTES // (1024*1024)):
        self.cam = cam
        self.cam_id = cam["id"]
        self.ffmpeg_path = ffmpeg_path
        # See MEM_RSS_CEILING_MB / _check_memory() — configurable via
        # recording.mem_rss_ceiling_mb (Ayarlar > Sistem) since the right
        # value depends on the host's total RAM budget, not something one
        # hardcoded default fits for both an SBC and a beefy NAS.
        self.mem_ceiling_mb = max(32, int(mem_ceiling_mb))
        self.segment_seconds = max(30, int(segment_seconds))
        self.rtsp_url = rtsp_url
        self.storage = storage
        self.proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.pattern: Optional[str] = None
        # tmpfs staging is a hard requirement, not a degradable feature —
        # there is no direct-to-disk fallback (see TMPFS_STAGE_ROOT's
        # comment and start()): if /tmp isn't confirmed tmpfs or doesn't
        # have self.tmpfs_safety_margin_bytes free right now, start()
        # simply refuses to start ffmpeg for this camera and the
        # supervisor retries on its next tick — recording resumes on its
        # own the moment tmpfs is usable again, with nothing to clean up
        # or reconcile. This is deliberately simpler and more predictable
        # than a silent degrade-to-disk mode: a broken/absent tmpfs is a
        # visible "not recording" condition (logged, see
        # _last_stage_block_log below) instead of an invisible change in
        # write behavior. Configurable via recording.tmpfs_safety_margin_mb
        # / tmpfs_hard_cap_mb (Ayarlar > Kayıt & Depolama) — the right
        # values depend on segment_seconds and the cameras' bitrates (a
        # long segment_seconds or a high-bitrate stream needs more RAM
        # headroom than the 256/512 MB defaults assume), not something one
        # hardcoded pair fits universally.
        self.tmpfs_safety_margin_bytes = max(1, int(tmpfs_safety_margin_mb)) * 1024**2
        self.tmpfs_hard_cap_bytes = max(1, int(tmpfs_hard_cap_mb)) * 1024**2
        self.stage_dir: Optional[Path] = None  # set once tmpfs is confirmed usable; None whenever not recording
        # Last time start() logged a "can't start, tmpfs unavailable"
        # message for this camera — the supervisor retries every
        # SUPERVISOR_INTERVAL (3s), so without this a persistently
        # unavailable tmpfs would flood the log forever. See start().
        self._last_stage_block_log: float = 0.0
        # Human-readable reason THIS camera isn't recording right now
        # (tmpfs unavailable, RAM below margin), or None while running
        # normally. Unlike the rate-limited log above, this always
        # reflects the CURRENT truth immediately — status() surfaces it so
        # the UI/API can show WHY a camera says "not recording" instead of
        # last_error sitting on a stale (or empty) ffmpeg stderr line from
        # a previous session, since ffmpeg never even got spawned this
        # time. Cleared the moment start() gets far enough to spawn ffmpeg.
        self._blocked_reason: Optional[str] = None
        # True from the moment a tmpfs->disk move fails until the next
        # move succeeds. Lets the watcher retry every tick instead of
        # waiting a full segment_seconds (see _watcher_loop), and lets
        # _enforce_stage_cap()'s caller know there's genuine backlog
        # growth rather than just the one normal in-flight segment.
        self._move_stuck: bool = False
        # Fixed, not user-configurable — every recorder always writes MP4.
        # Kept as an attribute (rather than a "mp4" literal repeated at
        # each of its three call sites) purely so those stay in sync if
        # this ever needs to change.
        self.ext: str = "mp4"
        self.manual_until: float = 0.0  # unix ts; > now means manual override on
        self.trigger: str = "schedule"
        # Bounded deque is thread-safe for append/pop-left; the stderr
        # drainer thread and the status endpoint no longer race on a plain
        # list.
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._stderr_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Adaptive watcher scheduling — see module docstring and
        # _watcher_loop. Both reset on every start(); segment_seconds is
        # THIS recorder's own configured value, so a non-default duration
        # (or a different value per camera) is respected automatically.
        self._next_check_at: float = 0.0
        self._last_latest_started: Optional[float] = None
        # Set the first time the watcher notices a segment is overdue
        # (past its expected close with no rollover seen yet); cleared the
        # moment a rollover IS observed. None means "not currently
        # overdue". Backstop for a stuck ffmpeg that -timeout didn't
        # catch — see _is_stale().
        self._stale_since: Optional[float] = None
        # Memory watchdog state — see MEM_RSS_CEILING_MB / _check_memory().
        self._last_mem_restart_at: float = 0.0

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _log_stage_blocked(self, msg: str, *args):
        """Record why start() refused to run. self._blocked_reason always
        updates immediately (status() needs the current truth, not a
        rate-limited one), but the actual journalctl log line is
        rate-limited to once per STAGE_BLOCK_LOG_INTERVAL_SEC — without
        that cap, a persistently unavailable tmpfs would log this every
        SUPERVISOR_INTERVAL (3s) forever, since start() gets called again
        on every tick as long as is_running() stays False."""
        formatted = msg % args if args else msg
        self._blocked_reason = formatted
        now = time.time()
        if now - self._last_stage_block_log < STAGE_BLOCK_LOG_INTERVAL_SEC:
            return
        self._last_stage_block_log = now
        log.error("[%s] %s", self.cam_id, formatted)

    def start(self, trigger: str):
        with self._lock:
            if self.is_running(): return
            # Reclaim any files stranded in this camera's stage dir from a
            # previous session (crash, app/recorder restart) BEFORE
            # deciding whether tmpfs is usable now — otherwise they'd sit
            # unregistered in RAM forever, since nothing else scans this
            # directory, and this has to happen regardless of whether
            # tmpfs ends up usable THIS time.
            candidate_stage_dir = TMPFS_STAGE_ROOT / self.cam_id
            if candidate_stage_dir.is_dir():
                self._flush_stage_leftovers(candidate_stage_dir)
            self.stage_dir = None

            # tmpfs is a hard requirement — see __init__'s comment on
            # stage_dir/self.tmpfs_safety_margin_bytes. No direct-to-disk
            # fallback: if it isn't usable right now, this camera simply
            # doesn't record until it is. The supervisor calls start()
            # again on its own next tick (SUPERVISOR_INTERVAL, 3s) as long
            # as is_running() stays False, so recording resumes
            # automatically the moment tmpfs becomes available again —
            # nothing to reconcile, no stray fallback files to migrate.
            if not _is_tmpfs(TMPFS_STAGE_ROOT):
                self._log_stage_blocked(
                    "tmpfs stage path %s is not confirmed to be tmpfs (RAM) — "
                    "cannot record without it (check /tmp's mount)", TMPFS_STAGE_ROOT)
                return
            try:
                candidate_stage_dir.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(str(TMPFS_STAGE_ROOT)).free
            except Exception as e:
                self._log_stage_blocked("tmpfs stage unavailable (%s) — cannot record", e)
                return
            if free < self.tmpfs_safety_margin_bytes:
                self._log_stage_blocked(
                    "tmpfs stage has < %dMB free — cannot record",
                    self.tmpfs_safety_margin_bytes // (1024*1024))
                return
            self.stage_dir = candidate_stage_dir
            self._blocked_reason = None
            log.info("[%s] tmpfs stage active at %s (confirmed RAM, %.0f MB free)",
                      self.cam_id, candidate_stage_dir, free / (1024*1024))

            # Filename encodes each segment's own wall-clock start time via
            # ffmpeg's -strftime 1. Second resolution is unique because
            # segment_time >= 30 s.
            self.pattern = str(self.stage_dir / f"{self.cam_id}_%Y%m%d_%H%M%S.{self.ext}")
            audio = bool(self.cam.get("record_audio", False))
            cmd = [
                self.ffmpeg_path,
                "-hide_banner", "-loglevel", "warning", "-nostdin",
                # Shorter probe: RTSP is a known-good source, we don't need
                # ffmpeg's default 5 s codec-sniffing before starting.
                "-analyzeduration", "500000", "-probesize", "500000",
                "-fflags", "+genpts",
                "-rtsp_transport", "tcp",
                # Bounds how long a blocking socket read can wait (µs) on
                # the underlying tcp:// connection. Without this, a camera
                # that drops the connection uncleanly (reboot, brief
                # network loss) leaves ffmpeg blocked in read() forever —
                # the process never exits, so is_running() keeps reporting
                # healthy while zero new segments ever get produced. 20s
                # is generous for a real hiccup; a genuinely dead
                # connection now surfaces as an ffmpeg exit within a
                # bounded time, which the existing supervisor tick already
                # knows how to restart.
                "-timeout", "20000000",

                # Stamp packets with wall-clock arrival time instead of
                # trusting the RTP-derived PTS coming from go2rtc. Without
                # this, a brief upstream hiccup (go2rtc losing and
                # regaining its own connection to the camera) can hand
                # ffmpeg a PTS discontinuity WITHOUT ever closing our RTSP
                # session — is_running() stays True the whole time, so the
                # supervisor never restarts anything, but the -f segment
                # muxer (which cuts on elapsed PTS, not real time) reads
                # the jump as "segment_time already elapsed" and starts
                # cutting segments far too early/too often, or in the
                # other direction never notices a segment is overdue.
                # Confirmed via a go2rtc-based drop/reconnect simulation:
                # without this flag a 30s-configured segment length
                # degraded to a run of 16-23s segments after a 12s drop;
                # with it, segment length stayed close to configured.
                "-use_wallclock_as_timestamps", "1",

                # Explicit cap on ffmpeg's real-time input buffer. ffmpeg
                # already defaults to a small cap here, so this is a
                # defensive belt-and-suspenders measure (not a proven fix
                # for any specific leak) against one known category of
                # RTSP-source memory growth under packet jitter/loss — the
                # memory watchdog below is the actual guaranteed backstop
                # regardless of root cause.
                "-rtbufsize", "16M",
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
                # 48k, not a higher default: AAC-LC has a hard per-channel
                # bit-budget cap of 6144 bits/frame (ISO/IEC 14496-3),
                # independent of sample rate. At the 8kHz mono audio these
                # cameras actually send (1024-sample frame = 128ms), that
                # caps the achievable bitrate at 6144/0.128s = 48 kbps —
                # asking for more (96k) can never be delivered, ffmpeg
                # just silently clamps every frame back down to this same
                # ceiling and logs a warning on every single frame's
                # worth of encoding. 48k asks for exactly what's
                # achievable: same audio, no per-frame clamp warning spam.
                #
                # -use_wallclock_as_timestamps above stamps every packet
                # (video AND audio) with its real arrival time rather than
                # a synthesized clock. Video tolerates that fine since it's
                # stream-copied, but the audio ENCODER needs strictly
                # increasing input timestamps — RTSP network jitter alone
                # is enough to occasionally hand it a timestamp that's
                # equal to or behind the previous one, which surfaces as
                # constant "Non-monotonic DTS" / "Queue input is backward
                # in time" warnings once transcoding starts (self-
                # corrected by ffmpeg, but log-spammy and a source of tiny
                # cumulative A/V drift). aresample=async=1 absorbs that
                # jitter by resampling onto a smooth, monotonic timeline
                # before the AAC encoder ever sees it.
                cmd += ["-af", "aresample=async=1", "-c:a", "aac", "-b:a", "48k", "-ac", "1"]
            cmd += [
                "-f", "segment",
                "-segment_time", str(self.segment_seconds),
                "-segment_format", "mp4",
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
            self._last_latest_started = None
            self._stale_since = None
            # First segment can't possibly close before segment_seconds
            # has elapsed — no point checking the directory before then.
            self._next_check_at = self.started_at + self.segment_seconds - TRACK_MARGIN_SEC
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
                # Also into the real logs (journalctl), not just the
                # in-memory tail the status API exposes — otherwise
                # whatever ffmpeg actually says about a stream hiccup
                # (dropped frames, non-monotonic DTS, a segment muxer
                # re-opening early) is invisible outside the app's own
                # log viewer. -loglevel warning above means ffmpeg only
                # emits warnings/errors here, so this stays low-volume in
                # normal operation.
                log.warning("[%s] ffmpeg: %s", self.cam_id, line)
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
            # self.stage_dir is deliberately NOT cleared here (start()
            # resets it fresh on the next call) — the watcher loop keys
            # its stopped-recorder RAM-cap/stray-file-retry pass on it
            # still being set post-stop, so a segment that couldn't be
            # moved above (storage still full at the exact moment this
            # camera stopped) keeps getting cleaned up / retried even
            # while this recorder isn't running.

    def _scan_and_register(self, final: bool = False) -> bool:
        """Register newly-closed segments.

        Filenames encode each segment's own wall-clock start time (ffmpeg
        ``-strftime 1``). ``started_at`` is parsed from the filename; the
        end of a closed file equals the start of the next file (accurate
        to the second — no watcher-polling drift). Only files that were
        opened by THIS recorder instance are considered, so leftover files
        from a previous crashed process are never double-registered.

        Scans stage_dir — this session's tmpfs staging directory, the only
        place ffmpeg ever writes; see _finish_segment for what happens to
        a closed file.

        Returns True if the latest segment file changed since the last
        call (i.e. ffmpeg rolled over to a new one) — the watcher uses
        this to know when to jump back to sleeping until the NEXT
        expected close instead of retrying on every tick.
        """
        if not self.stage_dir or not self.started_at:
            return False
        # Only pick up files whose parsed start is >= this session's spawn
        # time — everything else belongs to a previous run and is already
        # (or will be) recorded through its own recorder or via rescan.
        session_start = self.started_at - 5  # 5s slack for clock jitter
        try:
            candidates = sorted(self.stage_dir.glob(f"{self.cam_id}_*.{self.ext}"))
        except Exception:
            return False
        files: list[tuple[Path, float]] = []
        for p in candidates:
            ts = _parse_fname_ts(p)
            if ts is None or ts < session_start:
                continue
            files.append((p, ts))
        if not files:
            return False

        latest_started = files[-1][1]
        rolled = (self._last_latest_started is not None and latest_started > self._last_latest_started)
        self._last_latest_started = latest_started

        for i, (f, started) in enumerate(files):
            is_latest = (i == len(files) - 1)
            if not (final or not is_latest):
                # Latest file is still being written; register on rollover.
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if not is_latest:
                ended = files[i+1][1]  # next segment's start = this one's close
            else:
                ended = st.st_mtime  # final flush
            self._finish_segment(f, started, ended)
        return rolled

    def _finish_segment(self, f: Path, started: float, ended: float) -> bool:
        """Common tail for a segment ffmpeg has finished writing, used by
        both the normal per-tick rollover path (_scan_and_register) and
        one-shot stranded-file recovery at start() (_flush_stage_
        leftovers): verify playability, move it to its real home if it
        isn't already there, then register.

        The destination is resolved FRESH here, every single call — never
        a value fixed back at start() — by asking storage for whichever
        root currently has room and today's date-stamped folder under it.
        This is what lets a camera's footage keep landing in the right
        place after its original root fills up (or the day rolls over)
        WITHOUT the recorder itself ever restarting: the live ffmpeg
        process only ever depends on tmpfs, which is unaffected by which
        physical disk is currently best, so there's nothing about a stale
        target to fix by restarting — this method just always asks fresh.

        Returns False (leaving f exactly where it is, to retry later) only
        on a move failure — every other outcome either succeeds or is a
        permanent skip (already registered under its final name)."""
        # Moving onto the real disk is itself a disk write that can fail
        # with ENOSPC exactly like a live ffmpeg write would — make sure
        # some root has room FIRST, not just after (see the free_up_for_
        # new_segment() call below, which only covers the NEXT segment
        # once this one has already landed). Without this, a segment
        # stuck because the disk was full when it first tried to move
        # would only get a retry opportunity from something else's luck.
        self.storage.free_up_for_new_segment()
        target_root = self.storage.pick_write_root()
        target_day_dir = _cam_dir(target_root, self.cam_id)
        final_path = f if f.parent.resolve() == target_day_dir.resolve() else (target_day_dir / f.name)
        final_path_str = str(final_path.resolve())
        if self.storage.get_segment_by_path(final_path_str):
            return True  # already registered — a retry after an earlier partial failure
        if ended <= started:
            try: sz = f.stat().st_size
            except OSError: sz = 0
            ended = started + max(1.0, sz / 500_000.0)
        playable = _mp4_has_moov(f)
        if not playable:
            log.warning("[%s] segment closed without a valid trailer (unplayable): %s", self.cam_id, f)
        if final_path != f:
            try:
                target_day_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(final_path))
            except Exception as e:
                log.warning("[%s] failed to move %s -> %s (%s); will retry",
                            self.cam_id, f, final_path, e)
                self._move_stuck = True
                return False
            self._move_stuck = False
        self.storage.register_segment(self.cam_id, final_path_str, started, ended,
                                       trigger=self.trigger, playable=playable)
        # This segment just closed — ffmpeg is already writing the next
        # one (or about to, for -f segment's continuous rotation). Make
        # sure some root has room for it now; a no-op unless every root
        # is currently below its margin.
        self.storage.free_up_for_new_segment()
        return True

    def _flush_stage_leftovers(self, stage_dir: Path):
        """Move+register any files left over in stage_dir from a previous
        session (crash, app/recorder restart). Called at the top of every
        start(), before deciding whether tmpfs is usable this time —
        unlike _scan_and_register's normal path this doesn't filter by
        session_start (these all predate self.started_at, which isn't
        even set yet at this point in start()). Without this, a file
        stranded here would never be registered (nothing else scans this
        directory) and would sit taking up RAM indefinitely."""
        try:
            candidates = sorted(stage_dir.glob(f"{self.cam_id}_*.{self.ext}"))
        except Exception:
            return
        if not candidates:
            return
        log.info("[%s] tmpfs stage: recovering %d leftover segment(s) from a previous session",
                  self.cam_id, len(candidates))
        for i, f in enumerate(candidates):
            started = _parse_fname_ts(f)
            if started is None:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            nxt = _parse_fname_ts(candidates[i+1]) if i + 1 < len(candidates) else None
            ended = nxt if nxt is not None else st.st_mtime
            self._finish_segment(f, started, ended)

    def _enforce_stage_cap(self):
        """Drop the OLDEST not-yet-moved segment(s) in this camera's tmpfs
        stage the moment total staged bytes exceed self.tmpfs_hard_cap_
        bytes. Called every watcher tick for every staging-active camera
        (see _watcher_loop) — never gated behind a restart, and never
        stops the recorder: continuous recording matters more than any
        single already-buffered-but-unmoved segment, so that segment is
        sacrificed instead (data lost, logged clearly).

        A closed segment normally moves out within one watcher tick of
        closing, so steady-state usage is at most ~one in-flight segment;
        sustained growth past the cap means every configured storage root
        is genuinely out of room or unreachable (free_up_for_new_segment()
        and the per-tick move retries in _finish_segment() have already
        been trying and failing) and continuing to buffer into RAM
        unbounded would risk OOMing the whole box — which WOULD stop
        every camera's recording, a far worse outcome than losing the
        single oldest segment. The current (most recent) file is never a
        candidate: ffmpeg may still have it open for writing."""
        if not self.stage_dir:
            return
        try:
            files = sorted(self.stage_dir.glob(f"{self.cam_id}_*.{self.ext}"),
                           key=lambda p: _parse_fname_ts(p) or 0)
        except Exception:
            return
        if len(files) <= 1:
            return
        try:
            sizes = [(p, p.stat().st_size) for p in files]
        except Exception:
            return
        total = sum(sz for _, sz in sizes)
        if total <= self.tmpfs_hard_cap_bytes:
            return
        idx = 0
        while total > self.tmpfs_hard_cap_bytes and idx < len(sizes) - 1:  # keep the latest, always
            victim, sz = sizes[idx]
            idx += 1
            try:
                victim.unlink(missing_ok=True)
            except Exception as e:
                log.warning("[%s] tmpfs stage cap: failed to drop %s (%s)", self.cam_id, victim, e)
                continue
            total -= sz
            log.error("[%s] tmpfs stage over cap (%d MB) — dropped unmoved segment %s "
                      "(%.0f MB, data lost — destination disk(s) can't keep up)",
                      self.cam_id, self.tmpfs_hard_cap_bytes // (1024*1024), victim.name, sz / (1024*1024))

    def _check_memory(self) -> bool:
        """True if this recorder's ffmpeg has grown past self.mem_ceiling_mb
        (Ayarlar > Sistem, per-instance — see __init__) and should be
        restarted. Cooldown-gated so a fast-re-leaking stream can't thrash
        restarts — see MEM_CHECK_COOLDOWN_SEC."""
        if not self.proc:
            return False
        now = time.time()
        if now - self._last_mem_restart_at < MEM_CHECK_COOLDOWN_SEC:
            return False
        rss = _proc_rss_mb(self.proc.pid)
        if rss is None or rss < self.mem_ceiling_mb:
            return False
        log.warning("[%s] ffmpeg RSS at %.0f MB (ceiling %d MB) — restarting to prevent OOM",
                   self.cam_id, rss, self.mem_ceiling_mb)
        self._last_mem_restart_at = now
        return True

    def _is_stale(self) -> bool:
        """True if a segment has been overdue for long enough that ffmpeg
        is almost certainly stuck rather than just running a bit behind —
        e.g. the camera dropped the connection uncleanly and ffmpeg is
        blocked in a read() the -timeout flag didn't cover. is_running()
        can't see this: the process never exited, it's just not doing
        anything."""
        if self._stale_since is None:
            return False
        overdue_for = time.time() - self._stale_since
        threshold = min(STALE_CEIL_SEC, max(STALE_FLOOR_SEC, self.segment_seconds * STALE_MULTIPLIER))
        if overdue_for < threshold:
            return False
        log.warning("[%s] no new segment for %.0fs (expected every ~%ds) — "
                   "ffmpeg looks stuck, restarting", self.cam_id, overdue_for, self.segment_seconds)
        return True

    def status(self) -> dict:
        return {
            "cam_id": self.cam_id,
            "running": self.is_running(),
            "started_at": self.started_at,
            "trigger": self.trigger,
            "manual_until": self.manual_until,
            "last_error": (self._stderr_tail[-1] if self._stderr_tail else ""),
            "rss_mb": (round(_proc_rss_mb(self.proc.pid), 1) if self.proc else None),
            # Why THIS camera isn't recording right now (tmpfs unavailable
            # / below RAM margin), or None while running normally — see
            # _log_stage_blocked(). Distinct from last_error: that field
            # is ffmpeg's own stderr, which is stale or empty whenever
            # ffmpeg never even got spawned this session.
            "blocked_reason": self._blocked_reason,
        }


class RecordingManager:
    def __init__(self, config_store, storage):
        self.cfg = config_store
        self.storage = storage
        self._recs: dict[str, CameraRecorder] = {}
        self._lock = threading.RLock()
        # cam_ids currently mid stop()/restart() *outside* self._lock (see
        # _claim_busy/_release_busy) -- reserves a cam_id against a second
        # concurrent operation without holding the manager-wide lock for
        # the ~7s a stop() can take. See _tick()'s comment for the race
        # this replaces holding the lock across stop() to prevent.
        self._busy: set[str] = set()
        self._stop = threading.Event()
        self._sup_thread: Optional[threading.Thread] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._reload_timer: Optional[threading.Timer] = None

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

        r.stop() can take up to ~7s (terminate -> SIGINT -> SIGKILL). This
        used to run inside self._lock, on the reasoning that if the pop
        released it before stop() finished, a tick landing in that window
        would see cam_id as absent, want it running, and start a brand-new
        CameraRecorder while the old ffmpeg process was still alive — two
        processes writing the exact same second-resolution filename. True,
        but holding the *manager-wide* lock for those ~7s also blocked
        every unrelated camera's supervisor/watcher work and every HTTP
        request reading recorder state (e.g. /api/recording/status) behind
        this one camera's shutdown — a real, reported cause of the whole
        page intermittently taking 15-20s to load.

        _busy (see _claim_busy/_release_busy) gets the same safety more
        cheaply: reserve cam_id *before* releasing the lock, so a tick
        landing mid-stop sees the reservation (not an absent recorder) and
        defers rather than building a second one. The reservation is what
        has to be atomic with the pop, not the stop() itself.
        """
        with self._lock:
            if not self._claim_busy(cam_id):
                return  # another operation already owns this cam_id right now
            r = self._recs.pop(cam_id, None)
            if not r:
                self._release_busy(cam_id)
                return
        try:
            r.stop()
        except Exception:
            pass
        finally:
            self._release_busy(cam_id)

    def _claim_busy(self, cam_id: str) -> bool:
        """Must be called under self._lock. True if cam_id was free and is
        now reserved for a stop()/restart() the caller is about to run
        outside the lock; False if something else already owns it (caller
        should skip/defer, not act on this cam_id right now)."""
        if cam_id in self._busy:
            return False
        self._busy.add(cam_id)
        return True

    def _release_busy(self, cam_id: str):
        with self._lock:
            self._busy.discard(cam_id)

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
        # One atomic critical section covers get-or-create AND start() —
        # not just the get-or-create — so two racing POSTs never both
        # build a recorder and orphan one of them, and so a reload_camera()
        # racing in after the get-or-create but before start() can't pop
        # this same recorder out from under the start() call below (same
        # class of race as _tick(), see its comment).
        with self._lock:
            if cam_id in self._busy:
                # reload_camera() (or a tick's restart/stop) is mid stop()
                # for this exact camera right now and may have already
                # popped it out of self._recs (see _claim_busy). Deciding
                # from self._recs.get(cam_id) here would see it as absent
                # and build+register a second, independent recorder while
                # the old one is still shutting down -- the same
                # double-recording/orphan race _tick()'s busy check exists
                # to prevent. There's nowhere to hang "start when the
                # ongoing op finishes" off of without a recorder object to
                # hang it on, so this has to reject rather than defer;
                # POSTing again a moment later (once the in-flight
                # stop/restart clears) succeeds normally.
                return False
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
                            "trigger": None, "manual_until": 0, "last_error": "",
                            "rss_mb": None, "blocked_reason": None})
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
        if not self.storage.has_usable_root():
            log.warning("no usable storage root — skipping recorder for %s", cam["id"])
            return None
        return CameraRecorder(
            cam=cam, ffmpeg_path=ffmpeg,
            segment_seconds=int(rc.get("segment_seconds", 300)),
            rtsp_url=self._rtsp_url(cam), storage=self.storage,
            mem_ceiling_mb=int(rc.get("mem_rss_ceiling_mb", MEM_RSS_CEILING_MB)),
            tmpfs_safety_margin_mb=int(rc.get("tmpfs_safety_margin_mb",
                                               TMPFS_STAGE_SAFETY_MARGIN_BYTES // (1024*1024))),
            tmpfs_hard_cap_mb=int(rc.get("tmpfs_hard_cap_mb",
                                          TMPFS_STAGE_HARD_CAP_BYTES // (1024*1024))),
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
        to_stop = []  # [(cam_id, recorder)], stopped outside the lock below
        with self._lock:
            active_ids = set(self._recs.keys())
            cam_ids = {c["id"] for c in cams}
            # Drop recorders for deleted cameras.
            for gone in active_ids - cam_ids:
                if not self._claim_busy(gone):
                    continue  # another operation already owns it; next tick will retry
                r = self._recs.pop(gone, None)
                if r:
                    to_stop.append((gone, r))
                else:
                    self._release_busy(gone)
        for gone, r in to_stop:
            try: r.stop()
            except Exception: pass
            finally: self._release_busy(gone)

        for cam in cams:
            cam_id = cam["id"]
            # The read of self._recs and the DECISION made from it must
            # happen as one atomic unit under self._lock — not just the
            # dict access — otherwise a reload_camera() racing in between
            # this loop's unlocked `self._recs.get(...)` read and its later
            # start()/stop() call could pop the SAME recorder out from
            # under it: the old recorder then gets a start() call after
            # it's no longer tracked anywhere (an orphaned ffmpeg process
            # nothing can ever stop again), while the very next tick builds
            # a second, independently-tracked recorder for the same camera
            # (double recording).
            #
            # What must NOT happen under the lock is the actual stop() a
            # restart needs -- it can take ~7s (terminate -> SIGINT ->
            # SIGKILL), and holding the manager-wide lock for that blocks
            # every other camera's tick plus any HTTP request reading
            # recorder state (e.g. /api/recording/status), which is exactly
            # what made the whole page intermittently take 15-20s to load.
            # _claim_busy reserves cam_id for the slow part *before* the
            # lock is released, so a concurrent reload_camera() or another
            # tick sees the reservation (not an absent/idle recorder) and
            # defers instead of racing it.
            do_restart = False
            do_stop = False
            with self._lock:
                if cam_id in self._busy:
                    # Something else (reload_camera(), or this same loop's
                    # restart/stop branch below on a previous cam_id — see
                    # _claim_busy) is mid stop() for this camera right now,
                    # possibly having already popped it out of self._recs.
                    # Deciding anything from self._recs.get(cam_id) here
                    # would see it as absent and build+start a replacement
                    # while the old one is still shutting down -- the exact
                    # double-recording/orphan race this whole scheme exists
                    # to prevent. Defer; the next tick (SUPERVISOR_INTERVAL
                    # later) re-evaluates once the busy flag clears.
                    continue
                want, trigger = (False, "")
                r = self._recs.get(cam_id)
                # The global switch gates everything, including manual —
                # it previously only gated the record_mode-based branch
                # below, so flipping recording.enabled off system-wide
                # (e.g. during disk-full maintenance) left an in-progress
                # manual recording running until its own timer expired.
                if recording_globally_on:
                    # Manual override wins regardless of schedule.
                    if r and r.manual_until > now:
                        want, trigger = True, "manual"
                    else:
                        want, trigger = self._wants_run(cam, now)
                if want:
                    if r is None:
                        r = self._build(cam)
                        if not r: continue
                        self._recs[cam_id] = r
                    if not r.is_running():
                        r.start(trigger=trigger)  # fast (just Popen) -- fine under the lock
                    else:
                        r.trigger = trigger
                        if r._check_memory() or r._is_stale():
                            if self._claim_busy(cam_id):
                                do_restart = True
                else:
                    if r and r.is_running():
                        if self._claim_busy(cam_id):
                            do_stop = True
            if do_restart:
                try:
                    r.stop()
                    r.start(trigger=trigger)
                finally:
                    self._release_busy(cam_id)
            elif do_stop:
                try:
                    r.stop()
                finally:
                    self._release_busy(cam_id)

    def _watcher_loop(self):
        while not self._stop.wait(WATCHER_INTERVAL):
            now = time.time()
            with self._lock:
                recs = list(self._recs.values())
            for r in recs:
                # Non-blocking: start()/stop() holds r._lock for as long
                # as its own work takes (stop()'s signal escalation can
                # run up to ~7s) — since this loop processes every
                # recorder sequentially in one shared thread, BLOCKING
                # here on one camera's lock would stall every other
                # camera's watcher tick behind it for that whole window,
                # exactly the kind of manager-wide stall the _busy/
                # claim_busy pattern elsewhere in this file exists to
                # avoid. Skipping this recorder for one tick (2s) when
                # its lock is already held is cheap and safe — start()/
                # stop() already fully re-derive whatever state they need.
                if not r._lock.acquire(blocking=False):
                    continue
                try:
                    # RAM safety net + stray-file retry run regardless of
                    # is_running() — a camera that stopped (schedule
                    # ended, manually stopped, record_mode flipped off)
                    # while every storage root was completely full leaves
                    # its not-yet-moved tmpfs segments stranded exactly
                    # where they were: nothing else ever revisits
                    # stage_dir for a recorder that isn't running, so
                    # without this an unlucky stop timing left that RAM
                    # held (uncapped) and that footage stuck (never
                    # retried) until — if ever — this camera started
                    # recording again.
                    if r.stage_dir:
                        r._enforce_stage_cap()
                        if not r.is_running():
                            try:
                                stray = next(r.stage_dir.glob(f"{r.cam_id}_*.{r.ext}"), None)
                            except Exception:
                                stray = None
                            if stray is not None:
                                r._flush_stage_leftovers(r.stage_dir)
                    if not r.is_running():
                        continue
                    # The due-time gate exists so an ordinary tick with
                    # nothing new to find is a free in-memory check, no
                    # I/O at all (see module docstring) -- but a segment
                    # that failed to move (_move_stuck) costs nothing to
                    # retry and never needs a restart to recover, so it's
                    # worth retrying every tick (2s) rather than waiting a
                    # full segment_seconds for the next due check.
                    if now < r._next_check_at and not r._move_stuck:
                        continue
                    rolled = r._scan_and_register(final=False)
                    if rolled:
                        r._stale_since = None
                    elif r._stale_since is None:
                        # First tick to find us past the expected close
                        # with no rollover yet — mark when the overdue
                        # window started. _is_stale() (checked from the
                        # supervisor) uses this to tell "ffmpeg running a
                        # bit behind" apart from "actually stuck".
                        r._stale_since = now
                    # Confirmed rollover: this recorder's own segment_seconds
                    # (whatever it's configured to) sets when to check again.
                    # No rollover yet (ffmpeg running a bit behind, or we
                    # landed slightly early): retry on the next cheap tick
                    # instead of waiting a full segment — self-corrects
                    # without ever drifting permanently off schedule.
                    r._next_check_at = (now + r.segment_seconds - TRACK_MARGIN_SEC) if rolled else (now + WATCHER_INTERVAL)
                except Exception as e:
                    log.debug("watcher %s: %s", r.cam_id, e)
                finally:
                    r._lock.release()
