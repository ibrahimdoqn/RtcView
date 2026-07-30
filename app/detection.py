"""ONVIF motion / person detection — thin integration layer around the
vendored ``tapo_detector`` package (see ``/tapo_detector`` at the repo
root). That package is used exactly as delivered; nothing in it is
modified. This module only adds what a multi-camera dashboard needs on
top of its single-camera ``TapoMotionPersonDetector``:

  - One instance per opted-in camera, supervised/rebuilt on config
    change (mirrors ``RecordingManager``'s pattern).
  - A "motion/person stopped" timeout watchdog, built by SAMPLING the
    package's public ``get_state()`` once a second. ``tapo_detector``
    only flips its state on an explicit camera event, so a camera that
    never sends the "stopped" edge (Tapo, per field testing) would
    otherwise stay "active" forever — this is layered on top rather
    than patched into the package.
  - Detection intervals persisted to storage for the playback timeline.
  - A per-camera status/log snapshot for the settings UI's debug panel,
    captured via a ``logging.Handler`` attached to ``tapo_detector``'s
    own logger — read-only introspection, does not touch its source.
  - A standalone "test connection" probe for the UI's test button, using
    onvif-zeep directly (independent of the vendored package, which has
    no synchronous one-off connectivity check in its public API).
"""
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

log = logging.getLogger("detection")

TAPO_IMPORT_ERROR = None
_TAPO_WSDL_DIR: Optional[str] = None
try:
    import tapo_detector
    from tapo_detector import TapoMotionPersonDetector
    ONVIF_AVAILABLE = True
    # onvif-zeep's WSDL files are a data directory shipped next to (not
    # inside) the installed `onvif` package, and that placement is known
    # to go missing depending on pip version/install method. Point the
    # standalone probe below at tapo_detector's own bundled copy instead
    # of trusting wherever pip happened to put onvif-zeep's.
    _TAPO_WSDL_DIR = os.path.join(os.path.dirname(os.path.abspath(tapo_detector.__file__)), "wsdl")
    if not os.path.isdir(_TAPO_WSDL_DIR):
        _TAPO_WSDL_DIR = None
except Exception as _e:
    ONVIF_AVAILABLE = False
    TAPO_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    log.warning("tapo_detector import failed: %s", TAPO_IMPORT_ERROR)

try:
    # Only used for the standalone "test connection" probe below — the
    # watcher itself never touches onvif/zeep directly, that's entirely
    # tapo_detector's job.
    from onvif import ONVIFCamera
    from zeep.transports import Transport
except Exception:
    ONVIFCamera = None
    Transport = None

# Reuse the day/time-window matcher from recorder.py rather than
# reimplementing it — same cross-module-private-import precedent already
# used by storage.py's rescan() (`from app.recorder import _parse_fname_ts`).
from app.recorder import _schedule_active

POLL_INTERVAL_SEC = 1.0     # how often we sample tapo_detector.get_state()
EXTEND_WRITE_MIN_INTERVAL = 2.0  # throttle "still active" DB writes
LOG_MAXLEN = 200
KIND_LABEL = {"motion": "Hareket", "person": "İnsan"}
_TAPO_LOGGER_NAME = "tapo_detector"

# tapo_detector/detector.py's exact (frozen, unmodified, vendored) log
# format strings — matched via LogRecord.msg (the raw format string, not
# the interpolated text) to derive connected/subscribed state without
# touching its source. Stable for as long as we ship this exact copy.
_MSG_CONNECT_OK = "Kameraya baglanildi: %s %s (fw %s)"
_MSG_CONNECT_FAIL = "Kameraya baglanilamadi: %s"
_MSG_SUB_OK = "PullPoint abonelik olusturuldu."
_MSG_SUB_FAIL = "Abonelik olusturulamadi: %s"

# Bounded HTTP timeouts for the standalone test probe only, so an
# unreachable camera fails fast with a clear error instead of hanging
# the request thread.
_PROBE_WSDL_TIMEOUT_SEC = 8
_PROBE_OPERATION_TIMEOUT_SEC = 20


def _group_notify_active(group: dict, ts: float) -> bool:
    """Notification config lives on the GROUP now, not the camera (a
    camera inherits from every group it belongs to). Precedence for a
    single group: an active manual "force on" window wins outright
    (that's the whole point of a manual override), then an active
    snooze suppresses it, then the normal enabled+schedule check —
    same semantics as record_schedule here: an EMPTY schedule means
    never, not "any time". _schedule_active already returns False for
    an empty/falsy schedule."""
    if float(group.get("notify_force_until", 0) or 0) > ts:
        return True
    if float(group.get("notify_snooze_until", 0) or 0) > ts:
        return False
    if not group.get("notify_enabled", True):
        return False
    return _schedule_active(group.get("notify_schedule") or [])


class _PerCameraLogCapture(logging.Handler):
    """Routes tapo_detector's log records to the owning watcher's rolling
    log. tapo_detector logs every camera through one shared module-level
    logger AND one shared thread name ("tapo-detector"), so the only way
    to tell camera A's lines from camera B's — without editing the
    vendored package — is by the OS thread id LogRecord already carries
    automatically."""

    def __init__(self, owner: "CameraEventWatcher"):
        super().__init__(level=logging.INFO)
        self._owner = owner

    def emit(self, record: logging.LogRecord):
        if record.thread != self._owner.thread_ident:
            return
        try:
            self._owner._on_log_record(record)
        except Exception:
            pass


class CameraEventWatcher:
    """Wraps one ``TapoMotionPersonDetector`` instance for a single
    camera. Built fresh (by DetectionManager) whenever the camera's
    config changes — it never re-reads config mid-flight."""

    def __init__(self, cam: dict, storage, config_store):
        self.cam = cam
        self.cam_id = cam["id"]
        self.storage = storage
        self.config_store = config_store
        self.enabled_kinds = {
            k for k in ("motion", "person") if cam.get(f"{k}_detection_enabled")
        }
        try:
            self.timeout_seconds = max(3, int(cam.get("motion_timeout_seconds", 15) or 15))
        except (TypeError, ValueError):
            self.timeout_seconds = 15

        self._det: Optional["TapoMotionPersonDetector"] = None
        self.thread_ident: Optional[int] = None
        self._log_handler: Optional[_PerCameraLogCapture] = None

        self._stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._log: deque = deque(maxlen=LOG_MAXLEN)
        self._open_ids = {"motion": None, "person": None}
        self._last_seen = {"motion": 0.0, "person": 0.0}
        self._last_db_write = {"motion": 0.0, "person": 0.0}
        # tapo_detector.get_state()["timestamp"] only advances when it
        # actually applies a real camera event — used to tell a fresh
        # True apart from a stale one frozen by a stuck connection.
        self._last_underlying_ts: Optional[str] = None
        self._state = {
            "connected": False,
            "subscribed": False,
            "last_error": "",
            "last_error_at": None,
            "started_at": time.time(),
            "motion_active": False,
            "person_active": False,
            "last_motion_at": None,
            "last_person_at": None,
            "last_event_at": None,
        }

    # ---------- lifecycle ----------
    def start(self):
        if not ONVIF_AVAILABLE:
            self._set(last_error=f"tapo_detector kurulu değil ({TAPO_IMPORT_ERROR})")
            self._log_line(f"tapo_detector kurulu değil: {TAPO_IMPORT_ERROR}")
            return
        if not self.enabled_kinds:
            return
        try:
            self._det = TapoMotionPersonDetector(
                ip=self.cam.get("onvif_host") or self.cam.get("host"),
                port=int(self.cam.get("onvif_port", 80) or 80),
                username=self.cam.get("onvif_user") or "",
                password=self.cam.get("onvif_pass") or "",
                pull_timeout=10.0,
                # max_unexpected_errors intentionally left at the vendored
                # package's own default — see tapo_detector/detector.py.
            )
            self._det.start()
        except Exception as e:
            self._set(last_error=f"{type(e).__name__}: {e}", last_error_at=time.time())
            self._log_line(f"tapo_detector başlatılamadı: {e}")
            self._det = None
            return
        # Read-only introspection of tapo_detector's own thread object —
        # its ident is unique per instance even though the *name* isn't,
        # and CPython guarantees .ident is set by the time start() returns.
        self.thread_ident = self._det._thread.ident if self._det._thread else None
        self._log_handler = _PerCameraLogCapture(self)
        logging.getLogger(_TAPO_LOGGER_NAME).addHandler(self._log_handler)
        self._stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name=f"detect-poll-{self.cam_id}")
        self._poll_thread.start()

    def stop(self):
        self._stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=6)
        if self._log_handler:
            logging.getLogger(_TAPO_LOGGER_NAME).removeHandler(self._log_handler)
            self._log_handler = None
        if self._det:
            try:
                self._det.stop(timeout=6)
            except Exception as e:
                log.warning("tapo_detector stop failed for %s: %s", self.cam_id, e)
        self._close_all_open(time.time())

    def is_running(self) -> bool:
        return self._poll_thread is not None and self._poll_thread.is_alive()

    # ---------- status / debug ----------
    def status(self) -> dict:
        with self._lock:
            d = dict(self._state)
            log_lines = list(self._log)
        d["cam_id"] = self.cam_id
        d["running"] = self.is_running()
        d["enabled"] = {k: (k in self.enabled_kinds) for k in ("motion", "person")}
        d["timeout_seconds"] = self.timeout_seconds
        d["log"] = log_lines
        return d

    def _log_line(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._log.append(f"[{ts}] {msg}")

    def _set(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    def _on_log_record(self, record: logging.LogRecord):
        """Mirror tapo_detector's own log lines into our per-camera log,
        and derive connected/subscribed flags from its (frozen, unchanged)
        format strings — see the module docstring for why this is safe."""
        self._log_line(record.getMessage())
        if record.msg == _MSG_CONNECT_OK:
            self._set(connected=True, last_error="")
        elif record.msg == _MSG_CONNECT_FAIL:
            self._set(connected=False, subscribed=False,
                      last_error=record.getMessage(), last_error_at=time.time())
        elif record.msg == _MSG_SUB_OK:
            self._set(subscribed=True)
        elif record.msg == _MSG_SUB_FAIL:
            self._set(subscribed=False,
                      last_error=record.getMessage(), last_error_at=time.time())
        elif record.levelno >= logging.WARNING:
            self._set(last_error=record.getMessage(), last_error_at=time.time())

    # ---------- detection state machine (sampling-based) ----------
    def _mark_active(self, kind: str, ts: float):
        with self._lock:
            was_active = self._state[f"{kind}_active"]
            self._state[f"{kind}_active"] = True
            self._state[f"last_{kind}_at"] = ts
            self._state["last_event_at"] = ts
            self._last_seen[kind] = ts
            det_id = self._open_ids.get(kind)
            last_write = self._last_db_write.get(kind, 0)
        if not was_active or det_id is None:
            new_id = self.storage.open_detection(self.cam_id, kind, ts)
            with self._lock:
                self._open_ids[kind] = new_id
                self._last_db_write[kind] = ts
            if not was_active:
                self._log_line(f"{KIND_LABEL[kind]} başladı")
                self._maybe_notify(kind, ts)
        elif ts - last_write >= EXTEND_WRITE_MIN_INTERVAL:
            self.storage.extend_detection(det_id, ts)
            with self._lock:
                self._last_db_write[kind] = ts

    def _maybe_notify(self, kind: str, ts: float):
        """Fires once per false->true edge (called only from the
        `not was_active` branch above — never on the periodic "still
        active" pokes). No check for "is detection enabled for this kind"
        is needed here: this method is only ever reachable for kinds in
        self.enabled_kinds, built once at construction from
        motion_detection_enabled/person_detection_enabled."""
        try:
            group_ids = self.cam.get("group_ids") or []
            if not group_ids:
                # No group = no notifications. Notification config lives
                # entirely on groups now; an ungrouped camera has nothing
                # to inherit from.
                return
            groups = {g["id"]: g for g in self.config_store.get_groups()}
            # A camera notifies if ANY of its groups says "active now" —
            # membership in one silenced group never mutes another.
            if not any(_group_notify_active(groups[gid], ts) for gid in group_ids if gid in groups):
                return
            self.storage.create_notification(self.cam_id, kind, ts)
        except Exception as e:
            log.debug("notify check failed for %s/%s: %s", self.cam_id, kind, e)

    def _close_kind(self, kind: str, end_ts: float, reason: str):
        with self._lock:
            was_active = self._state[f"{kind}_active"]
            det_id = self._open_ids[kind]
            self._open_ids[kind] = None
            self._state[f"{kind}_active"] = False
        if was_active:
            if det_id is not None:
                self.storage.extend_detection(det_id, end_ts)
            self._log_line(f"{KIND_LABEL[kind]} durdu ({reason})")

    def _check_timeouts(self, now: float):
        for kind in self.enabled_kinds:
            with self._lock:
                active = self._state[f"{kind}_active"]
                last_seen = self._last_seen[kind]
            if active and last_seen and (now - last_seen) > self.timeout_seconds:
                self._close_kind(kind, last_seen, f"{self.timeout_seconds}sn sessizlik")

    def _close_all_open(self, ts: float):
        for kind in ("motion", "person"):
            with self._lock:
                active = self._state.get(f"{kind}_active")
            if active:
                self._close_kind(kind, ts, "izleyici durduruldu")

    # ---------- polling loop ----------
    def _poll_loop(self):
        while not self._stop.wait(POLL_INTERVAL_SEC):
            try:
                self._tick()
            except Exception as e:
                log.exception("detect poll tick failed for %s: %s", self.cam_id, e)

    def _tick(self):
        if self._det is None:
            return
        state = self._det.get_state()
        now = time.time()
        # A True value only counts as fresh evidence of ongoing motion if
        # tapo_detector's own timestamp actually moved since our last
        # poll — i.e. it just applied a real event. If the underlying
        # pull loop is stuck (reconnecting, resubscribing, failing), its
        # state dict simply stops mutating and a stale True would
        # otherwise keep refreshing _last_seen forever, defeating the
        # "stopped after N seconds of silence" timeout entirely.
        ts_str = state.get("timestamp")
        fresh = ts_str is not None and ts_str != self._last_underlying_ts
        self._last_underlying_ts = ts_str
        if fresh:
            for kind in self.enabled_kinds:
                if state.get(kind):
                    self._mark_active(kind, now)
        self._check_timeouts(now)


class DetectionManager:
    """Starts/stops one CameraEventWatcher per opted-in camera and keeps
    them in sync with config changes (mirrors RecordingManager's
    supervisor pattern)."""

    SUPERVISOR_INTERVAL = 5

    def __init__(self, config_store, storage):
        self.cfg = config_store
        self.storage = storage
        self._watchers: dict[str, CameraEventWatcher] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sup_thread: Optional[threading.Thread] = None

    def start(self):
        with self._lock:
            if self._sup_thread and self._sup_thread.is_alive():
                return
            self._stop.clear()
            self._sup_thread = threading.Thread(
                target=self._supervisor_loop, daemon=True, name="detect-supervisor")
            self._sup_thread.start()
            log.info("DetectionManager started")

    def stop(self):
        self._stop.set()
        with self._lock:
            watchers = list(self._watchers.values())
            self._watchers.clear()
        for w in watchers:
            try: w.stop()
            except Exception as e: log.warning("stop failed for %s: %s", w.cam_id, e)
        if self._sup_thread:
            self._sup_thread.join(timeout=5)

    def reload_camera(self, cam_id: str):
        """Called when a camera's config changed (or it was deleted); the
        supervisor tick will rebuild a watcher for it if still wanted."""
        with self._lock:
            w = self._watchers.pop(cam_id, None)
        if w:
            try: w.stop()
            except Exception as e: log.warning("watcher stop failed for %s: %s", cam_id, e)

    @staticmethod
    def _wants_watcher(cam: dict) -> bool:
        return bool(cam.get("onvif_host")) and (
            cam.get("motion_detection_enabled") or cam.get("person_detection_enabled"))

    def _supervisor_loop(self):
        while not self._stop.wait(self.SUPERVISOR_INTERVAL):
            try:
                self._tick()
            except Exception as e:
                log.exception("detect supervisor tick failed: %s", e)

    def _tick(self):
        cams = self.cfg.get_cameras()
        cam_ids = {c["id"] for c in cams}
        with self._lock:
            active_ids = set(self._watchers.keys())
        for gone in active_ids - cam_ids:
            self.reload_camera(gone)
        for cam in cams:
            want = self._wants_watcher(cam)
            with self._lock:
                w = self._watchers.get(cam["id"])
            if want and w is None:
                nw = CameraEventWatcher(cam, self.storage, self.cfg)
                nw.start()
                with self._lock:
                    self._watchers[cam["id"]] = nw
            elif not want and w is not None:
                self.reload_camera(cam["id"])

    def status(self) -> dict:
        with self._lock:
            watchers = dict(self._watchers)
        out = {}
        for c in self.cfg.get_cameras():
            w = watchers.get(c["id"])
            if w:
                out[c["id"]] = w.status()
            else:
                enabled = {
                    "motion": bool(c.get("motion_detection_enabled")),
                    "person": bool(c.get("person_detection_enabled")),
                }
                reason = "" if not (enabled["motion"] or enabled["person"]) else (
                    "ONVIF host tanımlı değil" if not c.get("onvif_host") else "")
                out[c["id"]] = {
                    "cam_id": c["id"], "enabled": enabled, "running": False,
                    "connected": False, "subscribed": False,
                    "motion_active": False, "person_active": False,
                    "last_motion_at": None, "last_person_at": None, "last_event_at": None,
                    "last_error": reason, "last_error_at": None,
                    "timeout_seconds": int(c.get("motion_timeout_seconds", 15) or 15),
                    "log": [],
                }
        return out

    def test_connection(self, cam: dict) -> dict:
        """Synchronous one-off connectivity probe for a UI 'Test' button.
        Independent of tapo_detector (which has no public one-off check)
        — uses onvif-zeep directly, exactly like the watcher underneath
        tapo_detector does, with a bounded timeout so an unreachable
        camera fails fast."""
        if ONVIFCamera is None:
            return {"ok": False, "error": "onvif-zeep kurulu değil"}
        host = cam.get("onvif_host")
        if not host:
            return {"ok": False, "error": "ONVIF host tanımlı değil"}
        try:
            port = int(cam.get("onvif_port", 80) or 80)
            transport = None
            if Transport is not None:
                transport = Transport(timeout=_PROBE_WSDL_TIMEOUT_SEC,
                                      operation_timeout=_PROBE_OPERATION_TIMEOUT_SEC)
            kwargs = {"transport": transport}
            if _TAPO_WSDL_DIR:
                kwargs["wsdl_dir"] = _TAPO_WSDL_DIR
            onvif_cam = ONVIFCamera(host, port, cam.get("onvif_user", ""), cam.get("onvif_pass", ""),
                                     **kwargs)
            info = onvif_cam.create_devicemgmt_service().GetDeviceInformation()
            return {"ok": True, "device": f"{info.Manufacturer} {info.Model} (fw {info.FirmwareVersion})"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
