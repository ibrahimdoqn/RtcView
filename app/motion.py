"""ONVIF motion detection subscriptions.

Two transport strategies, tried in order per camera:

  1. PullPoint (WS-BaseNotification pull model). Preferred — we open a
     subscription against the camera and long-poll PullMessages. Works
     on most Hikvision / Dahua / Reolink / Amcrest hardware.

  2. BaseNotification push (WS-Notification Subscribe with a Consumer
     Reference). Tapo cameras answer "Device doesn't support service:
     pullpoint" and require this: the camera POSTs SOAP envelopes to
     a webhook URL we provide (http://<lan-ip>:<app-port>/onvif/motion
     /<cam_id>). The webhook is served by main.py and hands parsed
     motion events back to storage via record_or_extend_motion.

Timeout-extend model: cheap cameras (Tapo especially) only emit a
"motion started" ping while movement continues, never a "motion
ended". storage.record_or_extend_motion collapses those into a
single interval by pushing ends_at to now+MOTION_TIMEOUT_SEC on
every ping, so an interval closes naturally when the pings stop.
"""

import logging
import re as _re
import socket
import threading
import time
from typing import Optional

log = logging.getLogger("motion")

MOTION_TIMEOUT_SEC = 15.0        # ping ⇒ ends_at = now + this
PULL_TIMEOUT       = "PT30S"     # WS-BaseNotification duration
SUBSCRIBE_TERM     = "PT2M"      # renew subscription this often
BACKOFF_ON_ERR     = 5.0         # sleep after error before retry
RESUB_SEC          = 90          # re-subscribe / renew interval

ONVIF_IMPORT_ERROR = None
try:
    from onvif import ONVIFCamera
    ONVIF_AVAILABLE = True
except Exception as _e:
    ONVIF_AVAILABLE = False
    ONVIF_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

_MOTION_TOPIC_HINTS = (
    "motion", "cellmotion", "motionalarm", "motiondetect",
)


def _get_local_ip() -> Optional[str]:
    """Best-effort: which local address would we use to talk to the LAN.
    UDP socket, no packet sent — just triggers the OS routing table."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None
    finally:
        s.close()


def _is_motion_soap(body_bytes: bytes) -> bool:
    """Return True when this SOAP body carries a motion=true event.

    Tolerant parsing: any topic containing 'motion' AND either
    no IsMotion field (Tapo ping-style) OR IsMotion=true qualifies.
    IsMotion=false explicitly excludes.
    """
    try:
        text = body_bytes.decode("utf-8", "replace")
    except Exception:
        return False
    low = text.lower()
    if not any(h in low for h in _MOTION_TOPIC_HINTS):
        return False
    # Look for a bool value tied to a motion-like name.
    #   <tt:SimpleItem Name="IsMotion" Value="true"/>
    for m in _re.finditer(
        r'name\s*=\s*"([^"]+)"[^>]*value\s*=\s*"([^"]+)"',
        text, _re.IGNORECASE,
    ):
        name, val = m.group(1).lower(), m.group(2).lower()
        if name in ("ismotion", "state", "motion", "active"):
            return val == "true"
    # No explicit bool — topic alone triggers a motion (Tapo pattern).
    return True


class _CamState:
    """Live status for the /api/cameras/<id>/motion/status endpoint."""
    __slots__ = ("enabled", "subscribed", "last_event_at",
                 "last_error", "started_at", "transport")

    def __init__(self):
        self.enabled = False
        self.subscribed = False
        self.last_event_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self.transport: Optional[str] = None      # 'pullpoint' | 'basenotify'


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
        with self._lock:
            self._reconcile_locked()

    def reload_camera(self, cam_id: str):
        self.reload_all()

    # ---------- webhook entry (called from Flask route) ----------
    def notify_from_webhook(self, cam_id: str, body_bytes: bytes) -> bool:
        """Called by main.py's /onvif/motion/<cid> endpoint."""
        if not _is_motion_soap(body_bytes):
            return False
        st = self._state.get(cam_id)
        at = time.time()
        if st: st.last_event_at = at
        try:
            _mid, opened = self.storage.record_or_extend_motion(
                cam_id, at, MOTION_TIMEOUT_SEC,
            )
            if opened:
                log.info("[%s] motion started (webhook)", cam_id)
            return True
        except Exception as e:
            log.warning("[%s] webhook record failed: %s", cam_id, e)
            return False

    # ---------- status ----------
    def status(self, cam_id: str) -> dict:
        st = self._state.get(cam_id) or _CamState()
        latest = self.storage.latest_motion(cam_id)
        now = time.time()
        currently_active = bool(latest and latest["ended_at"] > now)
        return {
            "enabled":     st.enabled,
            "subscribed":  st.subscribed,
            "transport":   st.transport,
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
        state.transport = None
        stop = threading.Event()
        self._stops[cid] = stop
        t = threading.Thread(
            target=self._camera_loop, args=(cid, stop),
            daemon=True, name=f"motion-{cid}",
        )
        self._threads[cid] = t
        t.start()

    def _camera_loop(self, cid: str, stop: threading.Event):
        state = self._state.setdefault(cid, _CamState())
        while not stop.is_set() and not self._global_stop.is_set():
            if not ONVIF_AVAILABLE:
                state.last_error = f"onvif-zeep import: {ONVIF_IMPORT_ERROR}"
                stop.wait(30); continue
            cam = self._find_cam(cid)
            if not cam:
                return
            try:
                self._try_transports(cid, cam, stop, state)
            except Exception as e:
                state.subscribed = False
                state.last_error = f"{type(e).__name__}: {e}"
                log.debug("[%s] motion loop error: %s", cid, e)
            if not stop.is_set() and not self._global_stop.is_set():
                stop.wait(BACKOFF_ON_ERR)
        state.subscribed = False

    def _try_transports(self, cid, cam, stop, state):
        """Try PullPoint first; on 'device doesn't support pullpoint',
        fall back to BaseNotification push (Tapo path). Logs the
        fallback decision ONCE per camera lifetime — the outer retry
        loop reruns this method on every backoff, so leaving the log
        at INFO produced one line every 5 s until the camera answers."""
        onvif = self._connect(cam)
        events = onvif.create_events_service()
        try:
            self._run_pullpoint(cid, onvif, events, stop, state)
            return
        except Exception as e:
            msg = str(e).lower()
            if "pullpoint" in msg or "does not support" in msg or "not support" in msg:
                if state.transport != "basenotify":
                    log.info("[%s] pullpoint unsupported, falling back to base-notification", cid)
                state.last_error = None
            else:
                raise
        # Base-notification fallback.
        self._run_basenotify(cid, onvif, events, stop, state)

    def _connect(self, cam):
        host = cam.get("onvif_host") or cam.get("host")
        port = int(cam.get("onvif_port", 80))
        user = cam.get("onvif_user") or cam.get("username", "")
        pw   = cam.get("onvif_pass") or cam.get("password", "")
        if not host:
            raise RuntimeError("no ONVIF host configured")
        return ONVIFCamera(host, port, user, pw)

    # ---------- transport: PullPoint ----------
    def _run_pullpoint(self, cid, onvif, events, stop, state):
        pullpoint = onvif.create_pullpoint_service()
        try:
            req = events.create_type("CreatePullPointSubscription")
            req.InitialTerminationTime = SUBSCRIBE_TERM
            events.CreatePullPointSubscription(req)
        except Exception as e:
            log.debug("[%s] CreatePullPointSubscription rejected: %s", cid, e)
            # If the create call itself was rejected as unsupported,
            # bubble that up so the outer catches the fallback signal.
            raise
        state.transport = "pullpoint"
        state.subscribed = True
        state.last_error = None
        renew_at = time.time() + RESUB_SEC

        while not stop.is_set() and not self._global_stop.is_set():
            now = time.time()
            if now >= renew_at:
                try:
                    r_req = pullpoint.create_type("Renew")
                    r_req.TerminationTime = SUBSCRIBE_TERM
                    pullpoint.Renew(r_req)
                except Exception as e:
                    log.debug("[%s] Renew failed: %s — will resubscribe", cid, e)
                    return
                renew_at = now + RESUB_SEC

            try:
                req = pullpoint.create_type("PullMessages")
                req.Timeout = PULL_TIMEOUT
                req.MessageLimit = 50
                resp = pullpoint.PullMessages(req)
            except Exception as e:
                if "timeout" in str(e).lower() or "timedout" in str(e).lower():
                    continue
                raise

            for notif in getattr(resp, "NotificationMessage", []) or []:
                if _is_motion_notif_object(notif):
                    at = time.time()
                    state.last_event_at = at
                    try:
                        _mid, opened = self.storage.record_or_extend_motion(
                            cid, at, MOTION_TIMEOUT_SEC,
                        )
                        if opened:
                            log.info("[%s] motion started (pullpoint)", cid)
                    except Exception as e:
                        log.warning("[%s] record_motion failed: %s", cid, e)

    # ---------- transport: BaseNotification (webhook push) ----------
    def _run_basenotify(self, cid, onvif, events, stop, state):
        webhook = self._webhook_url(cid)
        if not webhook:
            raise RuntimeError("could not determine local webhook URL")
        # WS-BaseNotification's Subscribe operation lives in a different
        # WSDL than the ONVIF events service (the Events service only
        # exposes CreatePullPointSubscription). onvif-zeep binds the
        # BaseNotification WSDL under create_notification_service().
        try:
            notification = onvif.create_notification_service()
        except Exception as e:
            raise RuntimeError(f"create_notification_service failed: {e}")
        req = notification.create_type("Subscribe")
        req.ConsumerReference = {"Address": webhook}
        req.InitialTerminationTime = SUBSCRIBE_TERM
        notification.Subscribe(req)
        state.transport = "basenotify"
        state.subscribed = True
        state.last_error = None
        log.info("[%s] motion subscription (basenotify) → %s", cid, webhook)

        # No local pulling — the camera POSTs to /onvif/motion/<cid>,
        # which routes through Flask into notify_from_webhook. Loop
        # here just to re-subscribe periodically (a subscription is
        # only valid for SUBSCRIBE_TERM) and to watch the stop flag.
        while not stop.is_set() and not self._global_stop.is_set():
            stop.wait(RESUB_SEC)
            if stop.is_set() or self._global_stop.is_set(): return
            try:
                notification.Subscribe(req)
            except Exception as e:
                log.debug("[%s] resubscribe failed: %s", cid, e)
                return

    def _webhook_url(self, cid: str) -> Optional[str]:
        ip = _get_local_ip()
        if not ip: return None
        try:
            port = int(self.cfg.get_app().get("port", 5000))
        except Exception:
            port = 5000
        return f"http://{ip}:{port}/onvif/motion/{cid}"

    # ---------- helpers ----------
    def _find_cam(self, cam_id: str) -> Optional[dict]:
        for c in self.cfg.get_cameras():
            if c["id"] == cam_id: return c
        return None


def _is_motion_notif_object(notif) -> bool:
    """PullPoint returns zeep objects, not raw XML. Same tolerant rules
    as _is_motion_soap, applied to the object graph."""
    try:
        topic = str(getattr(notif, "Topic", None) or "").lower()
        if not any(h in topic for h in _MOTION_TOPIC_HINTS):
            return False
        msg = getattr(notif, "Message", None)
        if msg is None: return True
        inner = getattr(msg, "_value_1", None) or msg
        data = getattr(inner, "Data", None)
        if data is None: return True
        items = getattr(data, "SimpleItem", None) or []
        if not items: return True
        for it in items:
            name = str(getattr(it, "Name", "")).lower()
            val  = str(getattr(it, "Value", "")).lower()
            if val in ("true", "false") and name in ("ismotion", "state", "motion", "active"):
                return val == "true"
            if val == "true":
                return True
        return False
    except Exception:
        return False
