"""ONVIF motion detection subscriptions.

MotionManager keeps one background thread per opted-in camera. Each
thread creates an ONVIF PullPoint subscription against the camera's
events service and pulls messages in a loop, extracting motion
notifications and forwarding them to storage.record_or_extend_motion.

Tapo (and some other budget cameras) only emit a "motion started"
ping while movement is happening — never a "motion ended". The
storage helper's timeout-extend model handles that transparently:
each ping pushes the interval's ends_at further into the future by
MOTION_TIMEOUT_SEC, so an interval "closes" naturally when the
pings stop coming.
"""

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("motion")

MOTION_TIMEOUT_SEC = 15.0        # ping ⇒ ends_at = now + this
PULL_TIMEOUT       = "PT30S"     # WS-BaseNotification duration
SUBSCRIBE_TERM     = "PT2M"      # renew subscription this often
BACKOFF_ON_ERR     = 5.0         # sleep after error before retry

ONVIF_IMPORT_ERROR = None
try:
    from onvif import ONVIFCamera
    ONVIF_AVAILABLE = True
except Exception as _e:
    ONVIF_AVAILABLE = False
    ONVIF_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

# Topics that different vendors use for motion. We match on a substring
# so vendor-specific rule names (e.g. "MyRuleDetector") still count.
_MOTION_TOPIC_HINTS = (
    "motion", "cellmotion", "motionalarm", "motiondetect",
)


class _CamState:
    """Live status for the /api/cameras/<id>/motion/status endpoint."""
    __slots__ = ("enabled", "subscribed", "last_event_at",
                 "last_error", "started_at")

    def __init__(self):
        self.enabled = False
        self.subscribed = False
        self.last_event_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None


class MotionManager:
    def __init__(self, config_store, storage):
        self.cfg = config_store
        self.storage = storage
        self._threads: dict[str, threading.Thread] = {}
        self._stops:   dict[str, threading.Event] = {}
        self._state:   dict[str, _CamState] = {}
        self._lock = threading.RLock()
        self._global_stop = threading.Event()

    # ---------- lifecycle ----------
    def start(self):
        with self._lock:
            self._global_stop.clear()
            self._reconcile_locked()
        log.info("MotionManager started (ONVIF %s)",
                 "available" if ONVIF_AVAILABLE else "MISSING")

    def stop(self):
        self._global_stop.set()
        with self._lock:
            stops = list(self._stops.values())
            threads = list(self._threads.values())
            self._stops.clear()
            self._threads.clear()
        for s in stops: s.set()
        for t in threads:
            try: t.join(timeout=2)
            except Exception: pass

    def reload_all(self):
        """Called by /api/cameras/... after any camera edit — brings the
        thread set in line with which cameras have motion_detect_enabled."""
        with self._lock:
            self._reconcile_locked()

    def reload_camera(self, cam_id: str):
        self.reload_all()

    # ---------- status ----------
    def status(self, cam_id: str) -> dict:
        st = self._state.get(cam_id) or _CamState()
        latest = self.storage.latest_motion(cam_id)
        now = time.time()
        currently_active = bool(latest and latest["ended_at"] > now)
        return {
            "enabled":     st.enabled,
            "subscribed":  st.subscribed,
            "started_at":  st.started_at,
            "last_event_at": st.last_event_at,
            "last_error":  st.last_error,
            "onvif_available": ONVIF_AVAILABLE,
            "onvif_import_error": ONVIF_IMPORT_ERROR,
            "currently_active": currently_active,
            "latest_interval": latest,
        }

    # ---------- internals ----------
    def _reconcile_locked(self):
        cams = self.cfg.get_cameras()
        want = {c["id"] for c in cams if c.get("motion_detect_enabled")}
        active = set(self._threads.keys())
        # Stop threads we no longer want.
        for gone in active - want:
            ev = self._stops.pop(gone, None)
            if ev: ev.set()
            th = self._threads.pop(gone, None)
            if th:
                try: th.join(timeout=1)
                except Exception: pass
            if gone in self._state:
                self._state[gone].enabled = False
                self._state[gone].subscribed = False
        # Start threads for newly enabled cameras.
        by_id = {c["id"]: c for c in cams}
        for cid in want - active:
            cam = by_id.get(cid)
            if not cam: continue
            self._spawn(cam)

    def _spawn(self, cam: dict):
        cid = cam["id"]
        state = self._state.setdefault(cid, _CamState())
        state.enabled = True
        state.subscribed = False
        state.last_error = None
        state.started_at = time.time()
        stop = threading.Event()
        self._stops[cid] = stop
        t = threading.Thread(
            target=self._camera_loop, args=(cid, stop),
            daemon=True, name=f"motion-{cid}",
        )
        self._threads[cid] = t
        t.start()

    def _camera_loop(self, cid: str, stop: threading.Event):
        """Outer loop: recreates the subscription after any error."""
        state = self._state.setdefault(cid, _CamState())
        while not stop.is_set() and not self._global_stop.is_set():
            if not ONVIF_AVAILABLE:
                state.last_error = f"onvif-zeep import: {ONVIF_IMPORT_ERROR}"
                stop.wait(30); continue
            cam = self._find_cam(cid)
            if not cam:
                # camera deleted — stop
                return
            try:
                self._subscribe_and_pull(cid, cam, stop, state)
            except Exception as e:
                state.subscribed = False
                state.last_error = f"{type(e).__name__}: {e}"
                log.debug("[%s] motion loop error: %s", cid, e)
            if not stop.is_set() and not self._global_stop.is_set():
                stop.wait(BACKOFF_ON_ERR)
        state.subscribed = False

    def _subscribe_and_pull(self, cid: str, cam: dict,
                             stop: threading.Event, state: _CamState):
        host = cam.get("onvif_host") or cam.get("host")
        port = int(cam.get("onvif_port", 80))
        user = cam.get("onvif_user") or cam.get("username", "")
        pw   = cam.get("onvif_pass") or cam.get("password", "")
        if not host:
            raise RuntimeError("no ONVIF host configured")

        onvif = ONVIFCamera(host, port, user, pw)
        events = onvif.create_events_service()
        # PullPoint: the subscription lives on the camera; we pull.
        pullpoint = onvif.create_pullpoint_service()
        # Some devices require an explicit CreatePullPointSubscription.
        try:
            req = events.create_type("CreatePullPointSubscription")
            req.InitialTerminationTime = SUBSCRIBE_TERM
            events.CreatePullPointSubscription(req)
        except Exception as e:
            log.debug("[%s] CreatePullPointSubscription rejected: %s", cid, e)

        state.subscribed = True
        state.last_error = None
        renew_at = time.time() + 90     # renew every 90 s (< SUBSCRIBE_TERM)

        while not stop.is_set() and not self._global_stop.is_set():
            # Renew the subscription periodically.
            now = time.time()
            if now >= renew_at:
                try:
                    r_req = pullpoint.create_type("Renew")
                    r_req.TerminationTime = SUBSCRIBE_TERM
                    pullpoint.Renew(r_req)
                except Exception as e:
                    log.debug("[%s] Renew failed: %s — will resubscribe", cid, e)
                    return
                renew_at = now + 90

            try:
                req = pullpoint.create_type("PullMessages")
                req.Timeout = PULL_TIMEOUT
                req.MessageLimit = 50
                resp = pullpoint.PullMessages(req)
            except Exception as e:
                # Timeout is normal — anything else means the sub is dead.
                msg = str(e).lower()
                if "timeout" in msg or "timedout" in msg:
                    continue
                raise

            for notif in getattr(resp, "NotificationMessage", []) or []:
                if self._is_motion_true(notif):
                    at = time.time()
                    state.last_event_at = at
                    try:
                        _mid, opened = self.storage.record_or_extend_motion(
                            cid, at, MOTION_TIMEOUT_SEC,
                        )
                        if opened:
                            log.info("[%s] motion started", cid)
                    except Exception as e:
                        log.warning("[%s] record_motion failed: %s", cid, e)

    # ---------- helpers ----------
    def _find_cam(self, cam_id: str) -> Optional[dict]:
        for c in self.cfg.get_cameras():
            if c["id"] == cam_id: return c
        return None

    @staticmethod
    def _is_motion_true(notif) -> bool:
        """Return True when the notification is a motion-start.

        Two shapes are common:
          Topic contains 'Motion...' AND SimpleItem[IsMotion|State]=true
          Topic contains 'Motion...' AND no bool item present (Tapo-ish
            — the mere emission of the event means motion started).
        We accept both. False readings ('false', 'IsMotion=false') are
        explicitly rejected so end-events don't retrigger the interval.
        """
        try:
            topic = str(getattr(notif, "Topic", None) or "")
            topic_low = topic.lower()
            if not any(h in topic_low for h in _MOTION_TOPIC_HINTS):
                return False
            # Walk SimpleItem elements looking for a bool state.
            msg = getattr(notif, "Message", None)
            if msg is None: return True  # topic-only motion → accept
            inner = getattr(msg, "_value_1", None) or msg
            data = getattr(inner, "Data", None)
            if data is None: return True
            items = getattr(data, "SimpleItem", None) or []
            if not items: return True
            found_bool = False
            for it in items:
                name = str(getattr(it, "Name", "")).lower()
                val  = str(getattr(it, "Value", "")).lower()
                if val in ("true", "false"):
                    found_bool = True
                    if name in ("ismotion", "state", "motion", "active"):
                        return val == "true"
                    # unknown bool — trust True as motion
                    if val == "true": return True
            # Bool items existed but none matched a known name → no
            # positive assertion; treat as not-motion.
            return not found_bool
        except Exception:
            return False
