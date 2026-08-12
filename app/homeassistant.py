"""Home Assistant motion / person / vehicle detection, and group
notification gating — both driven by entity state over one WebSocket.

Replaces the old ONVIF PullPoint-based detection engine entirely. Instead
of RtcView talking ONVIF events to each camera itself, each camera is
wired (in Ayarlar → Kameralar) to up to three Home Assistant
``binary_sensor`` entities — one each for motion / person / vehicle,
however HA's own object detection (Frigate, a camera's own integration,
whatever) chooses to expose them. RtcView just watches those entities'
on/off state and turns edges into the same detection intervals +
notifications the old system produced.

Whether a GROUP currently delivers notifications is decided the same
way: each group is wired (in Ayarlar → Bildirimler) to one HA
``input_boolean`` entity (e.g. ``input_boolean.fabrika_bildirim``), and
its live on/off state IS the answer — no schedule or manual switch lives
in RtcView any more. Turning notifications on/off, and any automatic
schedule for doing so, is entirely Home Assistant's job (e.g. an
automation flipping the input_boolean); this module just reads it.

Architecture
------------
ONE persistent WebSocket connection to the single configured HA instance
(``config.home_assistant``), not one per camera/group — HA already
multiplexes every entity's state over one connection, so there is no
reason to open several. ``HAManager`` owns that connection's background
thread and two watch maps rebuilt whenever camera/group config changes
(``reload()``): ``entity_id -> [(cam_id, kind), ...]`` for detection, and
``entity_id -> [group_id, ...]`` for notification gating.

On connect: authenticate, fetch every entity's current state once
(``get_states``) and reconcile it against in-memory state (this is also
what runs after every reconnect — see ``_resync`` below), then subscribe
to ``state_changed`` events and apply them live as they arrive.

On disconnect, in-memory "active" state and any already-open DB interval
are deliberately left untouched (we have no evidence anything stopped —
the failure is ours, not the sensor's); the next successful reconnect's
resync reconciles reality, extending a still-open interval or closing it
at the reconnect instant if HA now reports "off". This is the same
"best available evidence, not pretended precision" principle the old
ONVIF engine used for its silence-timeout fallback, just triggered by a
connection event instead of a timer. The same applies to a group's
notify-gate state: it simply keeps its last known value across a
disconnect and gets corrected on the next resync.
"""
import json
import logging
import ssl
import threading
import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

log = logging.getLogger("homeassistant")

WEBSOCKET_IMPORT_ERROR = None
try:
    import websocket  # websocket-client
    WEBSOCKET_AVAILABLE = True
except Exception as _e:                                   # pragma: no cover
    WEBSOCKET_AVAILABLE = False
    WEBSOCKET_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    websocket = None
    log.warning("websocket-client import failed: %s", WEBSOCKET_IMPORT_ERROR)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
KINDS = ("motion", "person", "vehicle")
KIND_LABEL = {"motion": "Hareket", "person": "İnsan", "vehicle": "Araç"}

# Recv-loop socket timeout AND the keepalive ping interval — if nothing
# (event or otherwise) arrives for this long, a ping is sent to check the
# connection is still alive rather than sitting blind indefinitely.
WS_IDLE_TIMEOUT_SEC = 30.0
# How long to wait for a ping's pong before declaring the connection dead.
PING_REPLY_TIMEOUT_SEC = 10.0

RECONNECT_BACKOFF_START_SEC = 2.0
RECONNECT_BACKOFF_MAX_SEC = 30.0

# Bounded timeouts for the one-shot REST helpers (entity list, connection
# test) so an unreachable HA instance fails fast instead of hanging a
# request thread.
REST_TIMEOUT_SEC = 8

LOG_MAXLEN = 200


def _ws_url(base_url: str) -> str:
    """http(s)://host:port -> ws(s)://host:port/api/websocket."""
    p = urlparse(base_url.strip())
    scheme = "wss" if p.scheme == "https" else "ws"
    netloc = p.netloc or p.path  # tolerate a bare "host:port" with no scheme
    return f"{scheme}://{netloc}/api/websocket"


def _rest_base(base_url: str) -> str:
    p = urlparse(base_url.strip())
    if not p.scheme:
        base_url = "http://" + base_url.strip()
        p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}"


def fetch_entities(url: str, token: str, verify_ssl: bool = True,
                    domain: str = "binary_sensor") -> dict:
    """One-shot REST call for the camera form's sensor picker: every
    entity_id in ``domain`` with its friendly name and current state."""
    url = (url or "").strip(); token = (token or "").strip()
    if not url or not token:
        return {"error": "Home Assistant URL/token tanımlı değil"}
    try:
        r = requests.get(
            f"{_rest_base(url)}/api/states",
            headers={"Authorization": f"Bearer {token}"},
            timeout=REST_TIMEOUT_SEC, verify=verify_ssl,
        )
        r.raise_for_status()
        states = r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    out = []
    prefix = domain + "."
    for st in states:
        eid = st.get("entity_id") or ""
        if not eid.startswith(prefix):
            continue
        attrs = st.get("attributes") or {}
        out.append({
            "entity_id": eid,
            "name": attrs.get("friendly_name") or eid,
            "state": st.get("state"),
            "device_class": attrs.get("device_class") or "",
        })
    out.sort(key=lambda e: e["name"].lower())
    return {"entities": out}


def test_connection(url: str, token: str, verify_ssl: bool = True) -> dict:
    """Quick REST healthcheck for the settings UI's 'Bağlantıyı Test Et'
    button — deliberately independent of the long-running WS connection."""
    url = (url or "").strip(); token = (token or "").strip()
    if not url:
        return {"ok": False, "error": "URL tanımlı değil"}
    if not token:
        return {"ok": False, "error": "Erişim jetonu (token) tanımlı değil"}
    try:
        r = requests.get(
            f"{_rest_base(url)}/api/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=REST_TIMEOUT_SEC, verify=verify_ssl,
        )
        if r.status_code == 401:
            return {"ok": False, "error": "Erişim jetonu geçersiz"}
        r.raise_for_status()
        return {"ok": True, "message": (r.json() or {}).get("message") or "Bağlantı başarılı"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class HAManager:
    """Owns the single background WebSocket connection to Home Assistant
    and the per-camera/per-kind detection state machine driven by it."""

    def __init__(self, config_store, storage):
        self.config_store = config_store
        self.storage = storage
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._next_msg_id = 1

        # entity_id -> [(cam_id, kind), ...] — rebuilt by reload().
        self._watched: Dict[str, List[Tuple[str, str]]] = {}
        # entity_id -> [group_id, ...] — rebuilt by reload(). Each watched
        # input_boolean's last-known on/off state lives in
        # _group_notify_state, keyed the same way (by entity_id, not
        # group_id, since several groups could in principle share one
        # entity and would then agree by construction).
        self._notify_watched: Dict[str, List[str]] = {}
        self._group_notify_state: Dict[str, bool] = {}

        # Per-camera state, same shape the old ONVIF watcher exposed (plus
        # a third "vehicle" kind) so the settings UI's debug panel needed
        # only additive changes, not a rewrite.
        self._cam_state: Dict[str, dict] = {}
        self._open_ids: Dict[Tuple[str, str], Optional[int]] = {}
        self._log: deque = deque(maxlen=LOG_MAXLEN)

        self._conn_state = {
            "connected": False, "subscribed": False,
            "last_error": "", "last_error_at": None,
        }

        self.reload()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        with self._lock:
            self._stop.clear()
            if WEBSOCKET_AVAILABLE:
                if not self._thread or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._run, daemon=True, name="ha-detect")
                    self._thread.start()
            else:
                self._note(f"websocket-client kurulu değil ({WEBSOCKET_IMPORT_ERROR})",
                           logging.WARNING)
        log.info("HAManager started")

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try: ws.close()
            except Exception: pass
        if self._thread:
            self._thread.join(timeout=WS_IDLE_TIMEOUT_SEC + 5)
        self._close_all_active(time.time(), reason="servis durduruldu")

    def reload(self):
        """Rebuild both watch maps from current camera/group config. Cheap
        and connection-agnostic — call after any camera or group
        add/update/delete."""
        watched: Dict[str, List[Tuple[str, str]]] = {}
        cam_ids_now = set()
        for cam in self.config_store.get_cameras():
            cam_ids_now.add(cam["id"])
            for kind in KINDS:
                eid = (cam.get(f"ha_{kind}_entity") or "").strip()
                if not eid:
                    continue
                watched.setdefault(eid, []).append((cam["id"], kind))
        notify_watched: Dict[str, List[str]] = {}
        for g in self.config_store.get_groups():
            eid = (g.get("ha_notify_entity") or "").strip()
            if eid:
                notify_watched.setdefault(eid, []).append(g["id"])
        with self._lock:
            self._watched = watched
            self._notify_watched = notify_watched
            # Drop state for cameras that no longer exist.
            for cid in list(self._cam_state.keys()):
                if cid not in cam_ids_now:
                    self._cam_state.pop(cid, None)
            for key in list(self._open_ids.keys()):
                if key[0] not in cam_ids_now:
                    self._open_ids.pop(key, None)
            # Drop cached state for entities no group watches any more.
            for eid in list(self._group_notify_state.keys()):
                if eid not in notify_watched:
                    self._group_notify_state.pop(eid, None)

    def reload_camera(self, cam_id: str):
        """Kept for call-site parity with the old DetectionManager API
        (main.py calls this on every camera add/update/delete) — a full
        reload() is cheap enough that a per-camera variant isn't worth
        the extra bookkeeping."""
        self.reload()

    # ------------------------------------------------------------------
    # Status / debug log
    # ------------------------------------------------------------------
    def _log_line(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._log.append(f"[{ts}] {msg}")

    def _note(self, msg: str, level: int = logging.INFO):
        self._log_line(msg)
        log.log(level, "%s", msg)

    def _fail(self, msg: str):
        self._note(msg, logging.WARNING)
        with self._lock:
            self._conn_state.update(last_error=msg, last_error_at=time.time())

    def _cam_default_state(self) -> dict:
        d = {"motion_active": False, "person_active": False, "vehicle_active": False,
             "last_motion_at": None, "last_person_at": None, "last_vehicle_at": None,
             "last_event_at": None}
        return d

    def status(self, cam_id: Optional[str] = None) -> dict:
        """Status keyed by camera id, mirroring the old DetectionManager's
        shape (plus a 'vehicle' kind) so the frontend needed only
        additive changes. Connection state is shared/global (one HA
        connection for the whole app) and repeated on every camera entry
        for backward-compatible UI code that reads it per-camera."""
        with self._lock:
            conn = dict(self._conn_state)
            log_lines = list(self._log)
        out = {}
        for cam in self.config_store.get_cameras():
            cid = cam["id"]
            if cam_id is not None and cid != cam_id:
                continue
            with self._lock:
                st = dict(self._cam_state.get(cid, self._cam_default_state()))
            enabled = {k: bool((cam.get(f"ha_{k}_entity") or "").strip()) for k in KINDS}
            entities = {k: (cam.get(f"ha_{k}_entity") or "") for k in KINDS}
            st.update(conn)
            st["cam_id"] = cid
            st["enabled"] = enabled
            st["entities"] = entities
            st["running"] = self._thread is not None and self._thread.is_alive()
            st["log"] = log_lines
            out[cid] = st
        return out

    def connection_state(self) -> dict:
        """The shared HA connection's state, for UI surfaces (like the
        Bildirimler tab) that show it without needing a whole camera."""
        with self._lock:
            return dict(self._conn_state)

    def group_notify_active(self, group: dict) -> bool:
        """Whether this group currently delivers notifications: the live
        on/off state of its ha_notify_entity, nothing else. An unwired
        group (no entity picked), or a wired one whose state we haven't
        heard yet (HA unreachable, or not resynced since reload), both
        read as False — silence is the safe default, never a false
        notification sent because we assumed a switch we can't see."""
        eid = (group.get("ha_notify_entity") or "").strip()
        if not eid:
            return False
        with self._lock:
            return bool(self._group_notify_state.get(eid, False))

    def group_notify_states(self) -> Dict[str, bool]:
        """entity_id -> known on/off state, for anything that wants to
        report every watched notify entity at once rather than one group
        at a time."""
        with self._lock:
            return dict(self._group_notify_state)

    # ------------------------------------------------------------------
    # Detection state machine — one entry per (cam_id, kind)
    # ------------------------------------------------------------------
    def _mark_active(self, cam_id: str, kind: str, ts: float):
        with self._lock:
            st = self._cam_state.setdefault(cam_id, self._cam_default_state())
            was_active = st[f"{kind}_active"]
            st[f"{kind}_active"] = True
            st[f"last_{kind}_at"] = ts
            st["last_event_at"] = ts
            det_id = self._open_ids.get((cam_id, kind))
        if not was_active or det_id is None:
            new_id = self.storage.open_detection(cam_id, kind, ts)
            with self._lock:
                self._open_ids[(cam_id, kind)] = new_id
            if not was_active:
                self._note(f"[{cam_id}] {KIND_LABEL[kind]} başladı")
                self._maybe_notify(cam_id, kind, ts)
        else:
            self.storage.extend_detection(det_id, ts)

    def _close_kind(self, cam_id: str, kind: str, end_ts: float, reason: str):
        with self._lock:
            st = self._cam_state.setdefault(cam_id, self._cam_default_state())
            was_active = st[f"{kind}_active"]
            det_id = self._open_ids.get((cam_id, kind))
            self._open_ids[(cam_id, kind)] = None
            st[f"{kind}_active"] = False
        if was_active and det_id is not None:
            self.storage.extend_detection(det_id, end_ts)
        if was_active:
            self._note(f"[{cam_id}] {KIND_LABEL[kind]} durdu ({reason})")

    def _close_all_active(self, ts: float, reason: str):
        with self._lock:
            cam_ids = list(self._cam_state.keys())
        for cid in cam_ids:
            for kind in KINDS:
                self._close_kind(cid, kind, ts, reason)

    def _maybe_notify(self, cam_id: str, kind: str, ts: float):
        """Fires once per false->true edge — mirrors the old ONVIF
        engine's _maybe_notify exactly, just keyed by cam_id/kind instead
        of belonging to a per-camera watcher object."""
        try:
            cam = next((c for c in self.config_store.get_cameras() if c["id"] == cam_id), None)
            if not cam:
                return
            group_ids = cam.get("group_ids") or []
            if not group_ids:
                return
            groups = {g["id"]: g for g in self.config_store.get_groups()}
            if not any(self.group_notify_active(groups[gid])
                       for gid in group_ids if gid in groups):
                return
            self.storage.create_notification(cam_id, kind, ts)
        except Exception as e:
            log.debug("notify check failed for %s/%s: %s", cam_id, kind, e)

    # ------------------------------------------------------------------
    # WebSocket connection
    # ------------------------------------------------------------------
    def _next_id(self) -> int:
        with self._lock:
            self._next_msg_id += 1
            return self._next_msg_id

    def _sleep_interruptible(self, seconds: float):
        self._stop.wait(seconds)

    def _run(self):
        backoff = RECONNECT_BACKOFF_START_SEC
        while not self._stop.is_set():
            try:
                self._connect_and_listen()
                backoff = RECONNECT_BACKOFF_START_SEC  # a session that ran means HA/network are fine
            except Exception as e:
                self._fail(f"Bağlantı hatası: {type(e).__name__}: {e}")
            with self._lock:
                self._conn_state.update(connected=False, subscribed=False)
            if self._stop.is_set():
                break
            self._sleep_interruptible(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SEC)

    def _connect_and_listen(self):
        cfg = self.config_store.get_home_assistant()
        url = (cfg.get("url") or "").strip()
        token = (cfg.get("token") or "").strip()
        if not url or not token:
            with self._lock:
                self._conn_state.update(last_error="Home Assistant URL/token tanımlı değil")
            # Not a real error worth logging every retry — just wait out
            # a full backoff cycle and check again (config may get set
            # from the UI at any time).
            raise RuntimeError("yapılandırılmadı")

        sslopt = None if cfg.get("verify_ssl", True) else {"cert_reqs": ssl.CERT_NONE}
        ws = websocket.create_connection(_ws_url(url), timeout=WS_IDLE_TIMEOUT_SEC, sslopt=sslopt)
        self._ws = ws
        try:
            first = json.loads(ws.recv())
            if first.get("type") != "auth_required":
                raise RuntimeError(f"beklenmeyen ilk mesaj: {first.get('type')}")
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            resp = json.loads(ws.recv())
            if resp.get("type") != "auth_ok":
                raise RuntimeError(f"kimlik doğrulama başarısız: {resp.get('message') or resp.get('type')}")
            self._note("Home Assistant'a bağlanıldı ve doğrulandı.")
            with self._lock:
                self._conn_state.update(connected=True, last_error="")

            self._resync(ws)

            sub_id = self._next_id()
            ws.send(json.dumps({"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}))
            sub_ack = json.loads(ws.recv())
            if sub_ack.get("id") != sub_id or not sub_ack.get("success"):
                raise RuntimeError(f"state_changed aboneliği başarısız: {sub_ack}")
            self._note("Durum değişikliklerine abone olundu.")
            with self._lock:
                self._conn_state.update(subscribed=True)

            last_ping_at = time.time()
            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    now = time.time()
                    if now - last_ping_at >= WS_IDLE_TIMEOUT_SEC:
                        if not self._ping(ws):
                            raise RuntimeError("keepalive ping yanıtsız — bağlantı ölü sayılıyor")
                        last_ping_at = now
                    continue
                if not raw:
                    continue
                self._handle_raw(raw, time.time())
        finally:
            with self._lock:
                self._conn_state.update(connected=False, subscribed=False)
            self._ws = None
            try: ws.close()
            except Exception: pass

    def _ping(self, ws) -> bool:
        ping_id = self._next_id()
        ws.send(json.dumps({"id": ping_id, "type": "ping"}))
        ws.settimeout(PING_REPLY_TIMEOUT_SEC)
        try:
            reply = json.loads(ws.recv())
            return reply.get("id") == ping_id and reply.get("type") == "pong"
        except Exception:
            return False
        finally:
            ws.settimeout(WS_IDLE_TIMEOUT_SEC)

    def _resync(self, ws):
        """Full state fetch, applied through the same _mark_active/
        _close_kind machinery a live event would use — this is what
        reconciles reality after any reconnect (including the very first
        connect), whether that means confirming "still active" (extends
        the already-open interval rather than starting a new one) or
        discovering a change that happened entirely during the outage.
        Also reconciles every watched group-notify entity's on/off state
        the same way."""
        req_id = self._next_id()
        ws.send(json.dumps({"id": req_id, "type": "get_states"}))
        result = json.loads(ws.recv())
        if result.get("id") != req_id or not result.get("success"):
            log.warning("get_states failed during resync: %s", result)
            return
        now = time.time()
        with self._lock:
            watched = dict(self._watched)
            notify_watched = dict(self._notify_watched)
        for st in result.get("result", []):
            eid = st.get("entity_id")
            active = st.get("state") == "on"
            targets = watched.get(eid)
            if targets:
                for cam_id, kind in targets:
                    if active:
                        self._mark_active(cam_id, kind, now)
                    else:
                        self._close_kind(cam_id, kind, now, "HA senkronizasyonu")
            if eid in notify_watched:
                with self._lock:
                    self._group_notify_state[eid] = active

    def _handle_raw(self, raw: str, now: float):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("type") != "event":
            return
        event = msg.get("event") or {}
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        with self._lock:
            targets = list(self._watched.get(entity_id) or [])
            is_notify_entity = entity_id in self._notify_watched
        new_state = (data.get("new_state") or {}).get("state")
        active = new_state == "on"
        for cam_id, kind in targets:
            if active:
                self._mark_active(cam_id, kind, now)
            else:
                self._close_kind(cam_id, kind, now, "HA olayı")
        if is_notify_entity:
            with self._lock:
                self._group_notify_state[entity_id] = active
