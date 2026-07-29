"""ONVIF motion / person event detection.

Ports the field-tested approach from the Tapo C520WS reference script:
edge-triggered PullPoint polling for exactly two verified topics
(``CellMotionDetector/Motion`` and ``PeopleDetector/People``), tolerant of
the camera's frequent benign ``RemoteDisconnected`` hiccups, with a
resubscribe → reconnect escalation when errors pile up.

On top of that, this module adds what a dashboard needs that a one-shot
CLI script doesn't:
  - Per-camera opt-in (``motion_detection_enabled`` / ``person_detection_enabled``).
  - Detection intervals persisted to ``storage`` (detections table) so the
    playback timeline can be colored after the fact.
  - A watchdog that treats "no new event for N seconds" as "detection
    stopped", because several cameras (Tapo firmware observed in
    practice) only ever send the start edge and never report the stop.
  - A small in-memory status snapshot (connection/subscription state,
    last error, last events, a rolling log) for the camera settings UI's
    debug panel.
"""
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("detection")

ONVIF_IMPORT_ERROR = None
try:
    from onvif import ONVIFCamera
    from zeep.transports import Transport
    ONVIF_AVAILABLE = True
except Exception as _e:
    ONVIF_AVAILABLE = False
    ONVIF_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    log.warning("onvif-zeep import failed: %s", ONVIF_IMPORT_ERROR)

try:
    import lxml.etree as ET
except Exception:
    ET = None

EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"

# topic substring -> internal kind. Only these two are field-verified
# (see the Tapo C520WS reference doc); anything else is ignored.
KIND_TOPICS = {
    "motion": "CellMotionDetector/Motion",
    "person": "PeopleDetector/People",
}
KIND_VALUE_KEY = {"motion": "IsMotion", "person": "IsPeople"}
KIND_LABEL = {"motion": "Hareket", "person": "İnsan"}

PULL_TIMEOUT_SEC = 10
MAX_CONSECUTIVE_ERRORS = 15     # genuinely unexpected errors -> resubscribe
MAX_BENIGN_ERRORS = 300         # RemoteDisconnected etc. -> much higher tolerance
RESUBSCRIBE_WAIT_SEC = 3
RECONNECT_WAIT_SEC = 5
EXTEND_WRITE_MIN_INTERVAL = 2.0  # throttle "still active" DB writes
LOG_MAXLEN = 200

# Bounded HTTP timeouts so a silently-unreachable camera (dropped packets,
# dead IP, firewall) fails fast with a clear error instead of hanging the
# watcher thread forever — a hung thread can't be stopped from Python and
# would leak on every subsequent camera edit. operation_timeout must stay
# comfortably above PULL_TIMEOUT_SEC since the camera legitimately holds
# the PullMessages request open for up to that long.
WSDL_TIMEOUT_SEC = 8
OPERATION_TIMEOUT_SEC = PULL_TIMEOUT_SEC + 10


def _make_transport():
    return Transport(timeout=WSDL_TIMEOUT_SEC, operation_timeout=OPERATION_TIMEOUT_SEC)


def _extract_topic(msg) -> str:
    try:
        topic_obj = msg.Topic
        return topic_obj._value_1 if hasattr(topic_obj, "_value_1") else str(topic_obj)
    except Exception:
        return ""


def _extract_simple_items(msg) -> list:
    if ET is None:
        return []
    try:
        msg_content = msg.Message._value_1
        if not hasattr(msg_content, "iter"):
            return []
        items = []
        for el in msg_content.iter():
            tag = el.tag
            localname = ET.QName(tag).localname if isinstance(tag, str) else str(tag)
            if localname == "SimpleItem":
                items.append((el.get("Name"), el.get("Value")))
        return items
    except Exception:
        return []


class CameraEventWatcher:
    """Owns one ONVIF PullPoint subscription + background thread for a
    single camera. Built fresh (by DetectionManager) whenever the
    camera's config changes — it never re-reads config mid-flight."""

    def __init__(self, cam: dict, storage):
        self.cam = cam
        self.cam_id = cam["id"]
        self.storage = storage
        self.enabled_kinds = {
            k for k in ("motion", "person")
            if cam.get(f"{k}_detection_enabled")
        }
        try:
            self.timeout_seconds = max(3, int(cam.get("motion_timeout_seconds", 15) or 15))
        except (TypeError, ValueError):
            self.timeout_seconds = 15

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._log: deque = deque(maxlen=LOG_MAXLEN)
        self._open_ids = {"motion": None, "person": None}
        self._last_seen = {"motion": 0.0, "person": 0.0}
        self._last_db_write = {"motion": 0.0, "person": 0.0}
        self._state = {
            "connected": False,
            "subscribed": False,
            "device_info": "",
            "last_error": "",
            "last_error_at": None,
            "consecutive_errors": 0,
            "started_at": time.time(),
            "motion_active": False,
            "person_active": False,
            "last_motion_at": None,
            "last_person_at": None,
            "last_event_at": None,
        }

    # ---------- lifecycle ----------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"onvif-evt-{self.cam_id}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)
        self._close_all_open(time.time())

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

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
        log.info("[%s] %s", self.cam_id, msg)

    def _set(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    # ---------- detection state machine ----------
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
        elif ts - last_write >= EXTEND_WRITE_MIN_INTERVAL:
            self.storage.extend_detection(det_id, ts)
            with self._lock:
                self._last_db_write[kind] = ts

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

    def _mark_inactive(self, kind: str, ts: float):
        self._close_kind(kind, ts, "kamera bildirdi")

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

    def _match_kind(self, raw_topic: str) -> Optional[str]:
        for kind, key in KIND_TOPICS.items():
            if key in raw_topic:
                return kind
        return None

    def _handle_message(self, msg):
        kind = self._match_kind(_extract_topic(msg))
        if kind is None or kind not in self.enabled_kinds:
            return
        items = dict(_extract_simple_items(msg))
        val = items.get(KIND_VALUE_KEY[kind])
        if val is None:
            return
        ts = time.time()
        if str(val).strip().lower() == "true":
            self._mark_active(kind, ts)
        else:
            self._mark_inactive(kind, ts)

    # ---------- ONVIF plumbing ----------
    def _connect(self):
        try:
            host = self.cam.get("onvif_host") or self.cam.get("host")
            port = int(self.cam.get("onvif_port", 80) or 80)
            user = self.cam.get("onvif_user") or ""
            pw = self.cam.get("onvif_pass") or ""
            onvif_cam = ONVIFCamera(host, port, user, pw, transport=_make_transport())
            info = onvif_cam.create_devicemgmt_service().GetDeviceInformation()
            device_info = f"{info.Manufacturer} {info.Model} (fw {info.FirmwareVersion})"
            self._set(connected=True, device_info=device_info, last_error="")
            self._log_line(f"Bağlandı: {device_info}")
            return onvif_cam
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            self._set(connected=False, subscribed=False, last_error=err, last_error_at=time.time())
            self._log_line(f"Bağlantı hatası: {err}")
            return None

    def _create_pullpoint(self, onvif_cam):
        try:
            events_service = onvif_cam.create_events_service()
            events_service.CreatePullPointSubscription()
            pullpoint = onvif_cam.create_pullpoint_service()
            pull_messages_type = pullpoint.zeep_client.get_element(f"{{{EVENTS_NS}}}PullMessages")
            self._set(subscribed=True)
            return pullpoint, pull_messages_type
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            self._set(subscribed=False, last_error=err, last_error_at=time.time())
            self._log_line(f"Abonelik hatası: {err}")
            return None, None

    def _listen_loop(self, onvif_cam) -> bool:
        """Returns True if the watcher was stopped cleanly (stop event),
        False if the caller should reconnect from scratch."""
        pullpoint, pull_messages_type = self._create_pullpoint(onvif_cam)
        if pullpoint is None:
            return False
        self._log_line("Abonelik oluşturuldu, dinleniyor...")
        consecutive_errors = 0
        consecutive_benign = 0
        while not self._stop.is_set():
            try:
                req = pull_messages_type(
                    Timeout=timedelta(seconds=PULL_TIMEOUT_SEC), MessageLimit=50)
                response = pullpoint.PullMessages(req)
                consecutive_errors = 0
                consecutive_benign = 0
                self._set(connected=True, subscribed=True)
                if response and response.NotificationMessage:
                    for msg in response.NotificationMessage:
                        self._handle_message(msg)
                self._check_timeouts(time.time())
            except Exception as e:
                err_str = str(e)
                now = time.time()
                # This camera (Tapo firmware, per field testing) drops the
                # PullMessages connection frequently and harmlessly — do
                # NOT count these against the real-error threshold or the
                # watcher would resubscribe forever and never see events.
                if "RemoteDisconnected" in err_str or "Connection aborted" in err_str:
                    consecutive_benign += 1
                    self._check_timeouts(now)
                    if consecutive_benign >= MAX_BENIGN_ERRORS:
                        self._log_line(
                            f"{consecutive_benign} art arda bağlantı hatası, abonelik yenileniyor")
                        self._stop.wait(RESUBSCRIBE_WAIT_SEC)
                        return False
                    self._stop.wait(0.3)
                    continue
                consecutive_benign = 0
                consecutive_errors += 1
                self._set(last_error=f"{type(e).__name__}: {e}", last_error_at=now,
                          consecutive_errors=consecutive_errors)
                self._log_line(f"Pull hatası: {e}")
                self._check_timeouts(now)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self._log_line(
                        f"{consecutive_errors} art arda beklenmeyen hata, abonelik yenileniyor")
                    self._stop.wait(RESUBSCRIBE_WAIT_SEC)
                    return False
                self._stop.wait(0.3)
        return True

    def _run(self):
        if not ONVIF_AVAILABLE:
            self._set(last_error=f"onvif-zeep kurulu değil ({ONVIF_IMPORT_ERROR})")
            self._log_line(f"onvif-zeep kurulu değil: {ONVIF_IMPORT_ERROR}")
            return
        if not self.enabled_kinds:
            return
        while not self._stop.is_set():
            onvif_cam = self._connect()
            if onvif_cam is None:
                if self._stop.wait(RECONNECT_WAIT_SEC):
                    break
                continue
            clean = self._listen_loop(onvif_cam)
            if clean:
                break
        self._close_all_open(time.time())
        self._set(connected=False, subscribed=False)


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
                nw = CameraEventWatcher(cam, self.storage)
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
                    "connected": False, "subscribed": False, "device_info": "",
                    "motion_active": False, "person_active": False,
                    "last_motion_at": None, "last_person_at": None, "last_event_at": None,
                    "last_error": reason, "last_error_at": None,
                    "timeout_seconds": int(c.get("motion_timeout_seconds", 15) or 15),
                    "log": [],
                }
        return out

    def test_connection(self, cam: dict) -> dict:
        """Synchronous one-off connectivity probe for a UI 'Test' button —
        deliberately independent of any running watcher/subscription."""
        if not ONVIF_AVAILABLE:
            return {"ok": False, "error": f"onvif-zeep kurulu değil ({ONVIF_IMPORT_ERROR})"}
        host = cam.get("onvif_host")
        if not host:
            return {"ok": False, "error": "ONVIF host tanımlı değil"}
        try:
            port = int(cam.get("onvif_port", 80) or 80)
            onvif_cam = ONVIFCamera(host, port, cam.get("onvif_user", ""), cam.get("onvif_pass", ""),
                                     transport=_make_transport())
            info = onvif_cam.create_devicemgmt_service().GetDeviceInformation()
            return {"ok": True, "device": f"{info.Manufacturer} {info.Model} (fw {info.FirmwareVersion})"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
