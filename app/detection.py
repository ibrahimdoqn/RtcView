"""ONVIF motion / person detection — camera events to timeline + notifications.

This module is the whole detection subsystem: it talks ONVIF PullPoint
Events to each opted-in camera, turns those events into detection
intervals in the database (for the playback timeline), and raises
notifications. It replaces what used to be two layers — a vendored
``tapo_detector`` package that owned the ONVIF conversation, plus a thin
app-side wrapper that SAMPLED that package's state once a second and
guessed what had happened from it.

Why they were merged
--------------------
The old split had the app layer polling ``get_state()`` at 1 Hz and
deciding "did something happen?" by checking whether the underlying
detector's ISO-8601 timestamp string had changed. That indirection was
lossy in ways that mattered:

  * A "motion STOPPED" event (``IsMotion=false``) reached the lower layer
    correctly, but the sampling loop only ever acted on truthy values —
    so the stop edge was silently discarded and every interval had to
    wait for the silence timeout instead. Handling it is the main reason
    for this rewrite.
  * The timestamp had one-second resolution, so two events inside the
    same second were indistinguishable from no event at all.
  * Attributing log lines to a camera required matching the OS thread id
    against the detector's private ``_thread`` attribute, and deriving
    connected/subscribed state by string-matching frozen log format
    strings. Both broke silently if anything upstream changed.

Now there is one thread per camera that pulls events and applies them
directly, so nothing is inferred and nothing is sampled.

Hard-won ONVIF / onvif-zeep behaviour encoded here
--------------------------------------------------
Verified against real TP-Link Tapo hardware (C510W / C520WS); do not
"simplify" these away:

  1. ``CreatePullPointSubscription`` must be called on the **events**
     service, not the pullpoint service (otherwise: "Service has no
     operation").
  2. The ``PullMessages`` request type must be fetched by fully-qualified
     namespace, not the library's fragile ``ns0:`` alias (otherwise:
     "No element 'PullMessages' in namespace ...rw-2").
  3. Event ``Data`` is NOT parsed by zeep (``xs:any`` content model) — it
     arrives as a raw lxml Element and must be walked with lxml's own API.
  4. "RemoteDisconnected" / "Connection aborted" is NORMAL on this
     firmware, not fatal — it gets its own, far more tolerant counter.
  5. Some firmware reports a bogus ``SubscriptionReference.Address``
     (an internal-only port), which onvif-zeep would then cache as the
     pullpoint endpoint, making every pull fail forever. We overwrite it
     with the known-good Events XAddr before building the service.
  6. A subscription can go quiet camera-side without ever raising an
     error, so it is recreated on a fixed age schedule, not only on
     failure.
"""
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("detection")

ONVIF_IMPORT_ERROR = None
try:
    import lxml.etree as ET
    from onvif import ONVIFCamera
    from zeep.transports import Transport
    ONVIF_AVAILABLE = True
except Exception as _e:                                   # pragma: no cover
    ONVIF_AVAILABLE = False
    ONVIF_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    ET = None
    ONVIFCamera = None
    Transport = None
    log.warning("ONVIF stack import failed: %s", ONVIF_IMPORT_ERROR)

# Reuse the day/time-window matcher from recorder.py rather than
# reimplementing it — same cross-module-private-import precedent already
# used by storage.py's rescan() (`from app.recorder import _parse_fname_ts`).
from app.recorder import _schedule_active

# ---------------------------------------------------------------------------
# WSDL location
# ---------------------------------------------------------------------------
# onvif-zeep ships its WSDL/XSD files as a data directory that sits NEXT TO
# (not inside) the installed `onvif` package. Depending on the pip version
# and install method that placement can end up missing or unreadable, which
# surfaces as a permanent "wsdl bulunamadi" / endless pull failures. We ship
# our own copy at app/wsdl and always point the ONVIF client at it, so the
# client never depends on where pip happened to put onvif-zeep's data files.
#
# NOTE: the tree includes sub-directories (addressing/, envelope/, include/,
# xmlmime/) that the top-level WSDLs import — it must be kept whole. app/ptz.py
# resolves the same directory the same way; if this ever moves, update both.
ONVIF_WSDL_DIR: Optional[str] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wsdl")
if not os.path.isdir(ONVIF_WSDL_DIR):                     # pragma: no cover
    log.warning("Bundled WSDL directory missing: %s — falling back to onvif-zeep's own copy",
                ONVIF_WSDL_DIR)
    ONVIF_WSDL_DIR = None

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# How long the camera holds a PullMessages request open when it has nothing
# to report. This is ALSO the granularity of the silence-timeout check,
# because one thread does both: while a pull is blocked, no timeout can
# fire. Stored interval ends stay exact regardless (they are recorded at
# `last_seen`, not at the moment we notice) — only the live "hareket var"
# badge can linger up to this long past the timeout. Bigger = fewer HTTP
# round trips; smaller = snappier live status.
PULL_TIMEOUT_SEC = 5.0

# Maximum messages the camera may return in a single pull.
PULL_MESSAGE_LIMIT = 50

# Consecutive UNEXPECTED pull errors tolerated before forcing a resubscribe.
# Field logs showed these arriving in tight bursts that never self-heal
# inside a larger retry window — every burst ended in the same forced
# resubscribe anyway, just later and with far more log spam. Resubscribing
# on the very first one cuts both.
MAX_UNEXPECTED_ERRORS = 1

# Consecutive BENIGN errors (RemoteDisconnected / "Connection aborted")
# tolerated before resubscribing. Deliberately huge and deliberately
# separate from the counter above: on this firmware these are normal
# chatter — testing saw 30-40 of them before events started flowing — and
# they really do self-heal on retry.
MAX_BENIGN_ERRORS = 300

# Recreate the subscription once it reaches this age even if nothing has
# gone wrong. A PullPoint subscription can stop delivering events
# camera-side without ever raising an error; bounding how long any one
# subscription is trusted closes that window instead of only reacting
# after a detection gap has already happened.
MAX_SUBSCRIPTION_AGE_SEC = 300.0

# Pause after an error before retrying the pull.
ERROR_RETRY_SLEEP_SEC = 0.3

# Pause between tearing down a subscription and creating the next one.
# Kept short: this is dead time in which no event can be caught. (A real
# subscribe FAILURE backs off with CONNECT_RETRY_SLEEP_SEC instead.)
RESUBSCRIBE_GAP_SEC = 0.5

# Backoff after a failed connect or failed subscribe.
CONNECT_RETRY_SLEEP_SEC = 5.0

# Fallback used when a camera never sends a "stopped" edge at all. Real
# stop events are honoured the moment they arrive (see _handle_message);
# this only decides how long a kind stays "active" with no further
# evidence. Per-camera, overridable via motion_timeout_seconds.
MOTION_TIMEOUT_DEFAULT_SEC = 15
MOTION_TIMEOUT_MIN_SEC = 3

# Throttle for "still active" DB writes while motion continues — the
# camera re-asserts IsMotion=true far more often than the timeline needs.
EXTEND_WRITE_MIN_INTERVAL_SEC = 2.0

# Rolling per-camera log shown in the settings UI's debug panel.
LOG_MAXLEN = 200

# Bounded HTTP timeouts for the standalone "test connection" probe only,
# so an unreachable camera fails fast instead of hanging a request thread.
PROBE_WSDL_TIMEOUT_SEC = 8
PROBE_OPERATION_TIMEOUT_SEC = 20

# ---------------------------------------------------------------------------
# ONVIF constants
# ---------------------------------------------------------------------------
# Events service WSDL namespace — used to pull the PullMessages element by
# fully-qualified name (see module docstring, point 2).
_EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"

# Verified event topics on this hardware -> (state field, SimpleItem name).
# Keys are matched as SUBSTRINGS because topics arrive with prefixes such as
# "tns1:RuleEngine/CellMotionDetector/Motion".
_TOPIC_TO_FIELD = {
    "PeopleDetector/People": ("person", "IsPeople"),
    "CellMotionDetector/Motion": ("motion", "IsMotion"),
}

KIND_LABEL = {"motion": "Hareket", "person": "İnsan"}


def _group_notify_active(group: dict, ts: float) -> bool:
    """Notification config lives on the GROUP, not the camera (a camera
    inherits from every group it belongs to). Precedence for a single
    group: an active manual "force on" window wins outright (that's the
    whole point of a manual override), then an active snooze suppresses
    it, then the normal enabled+schedule check — same semantics as
    record_schedule here: an EMPTY schedule means never, not "any time".
    _schedule_active already returns False for an empty/falsy schedule."""
    if float(group.get("notify_force_until", 0) or 0) > ts:
        return True
    if float(group.get("notify_snooze_until", 0) or 0) > ts:
        return False
    if not group.get("notify_enabled", True):
        return False
    return _schedule_active(group.get("notify_schedule") or [])


class CameraEventWatcher:
    """Owns the full ONVIF event conversation for a single camera.

    One background thread per camera: connect -> subscribe -> pull events
    in a loop -> apply each event to the detection state machine. Built
    fresh by DetectionManager whenever the camera's config changes; it
    never re-reads camera config mid-flight.
    """

    def __init__(self, cam: dict, storage, config_store):
        self.cam = cam
        self.cam_id = cam["id"]
        self.storage = storage
        self.config_store = config_store
        self.enabled_kinds = {
            k for k in ("motion", "person") if cam.get(f"{k}_detection_enabled")
        }
        try:
            self.timeout_seconds = max(
                MOTION_TIMEOUT_MIN_SEC,
                int(cam.get("motion_timeout_seconds", MOTION_TIMEOUT_DEFAULT_SEC)
                    or MOTION_TIMEOUT_DEFAULT_SEC),
            )
        except (TypeError, ValueError):
            self.timeout_seconds = MOTION_TIMEOUT_DEFAULT_SEC

        # ONVIF handles (owned by the worker thread once started)
        self._cam: Optional["ONVIFCamera"] = None
        self._pullpoint = None
        self._pull_messages_type = None
        self._subscribed_at: float = 0.0

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._log: deque = deque(maxlen=LOG_MAXLEN)

        # Detection state machine bookkeeping
        self._open_ids: Dict[str, Optional[int]] = {"motion": None, "person": None}
        self._last_seen: Dict[str, float] = {"motion": 0.0, "person": 0.0}
        self._last_db_write: Dict[str, float] = {"motion": 0.0, "person": 0.0}

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if not ONVIF_AVAILABLE:
            self._set(last_error=f"ONVIF kütüphanesi kurulu değil ({ONVIF_IMPORT_ERROR})")
            self._log_line(f"ONVIF kütüphanesi kurulu değil: {ONVIF_IMPORT_ERROR}")
            return
        if not self.enabled_kinds:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"detect-{self.cam_id}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            # Generous relative to PULL_TIMEOUT_SEC: the thread may be
            # parked inside a blocking PullMessages call and can only
            # notice the stop flag once that returns.
            self._thread.join(timeout=PULL_TIMEOUT_SEC + 5)
        self._close_all_open(time.time())

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Status / debug log
    # ------------------------------------------------------------------
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
        """Append to the rolling per-camera log the settings UI shows."""
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._log.append(f"[{ts}] {msg}")

    def _note(self, msg: str, level: int = logging.INFO):
        """Record a line in BOTH the per-camera UI log and the service
        journal. Journal lines carry the camera id because several
        watchers run concurrently."""
        self._log_line(msg)
        log.log(level, "[%s] %s", self.cam_id, msg)

    def _fail(self, msg: str, **state):
        self._note(msg, logging.WARNING)
        self._set(last_error=msg, last_error_at=time.time(), **state)

    def _set(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    # ------------------------------------------------------------------
    # ONVIF: connect + subscribe
    # ------------------------------------------------------------------
    def _connect(self) -> bool:
        try:
            kwargs = {"wsdl_dir": ONVIF_WSDL_DIR} if ONVIF_WSDL_DIR else {}
            self._cam = ONVIFCamera(
                self.cam.get("onvif_host") or self.cam.get("host"),
                int(self.cam.get("onvif_port", 80) or 80),
                self.cam.get("onvif_user") or "",
                self.cam.get("onvif_pass") or "",
                **kwargs,
            )
            info = self._cam.create_devicemgmt_service().GetDeviceInformation()
            self._note(f"Kameraya bağlanıldı: {info.Manufacturer} {info.Model} "
                       f"(fw {info.FirmwareVersion})")
            self._set(connected=True, last_error="")
            return True
        except Exception as e:
            self._cam = None
            self._fail(f"Kameraya bağlanılamadı: {e}", connected=False, subscribed=False)
            return False

    def _create_pullpoint(self) -> bool:
        try:
            # Point 1: CreatePullPointSubscription lives on the EVENTS
            # service, not the pullpoint service.
            events_service = self._cam.create_events_service()
            events_service.CreatePullPointSubscription()

            # Point 5: onvif-zeep caches the pullpoint endpoint from the
            # camera's self-reported SubscriptionReference.Address (see
            # onvif.client.update_xaddrs). Some Tapo firmware reports a
            # bogus/unreachable address there (observed: an internal-only
            # ':1026') even though the Events service itself — used
            # successfully just above — is announced correctly via
            # GetCapabilities. Left alone, every PullMessages call fails
            # with "Connection refused" forever. Overwrite the cached
            # address with the known-good Events XAddr BEFORE building the
            # pullpoint service, so it is constructed correctly from the
            # start.
            pullpoint_ns = _EVENTS_NS + "/PullPointSubscription"
            events_xaddr = self._cam.xaddrs.get(_EVENTS_NS)
            if events_xaddr and self._cam.xaddrs.get(pullpoint_ns) != events_xaddr:
                self._cam.xaddrs[pullpoint_ns] = events_xaddr

            self._pullpoint = self._cam.create_pullpoint_service()

            # Point 2: bypass the fragile 'ns0' alias by resolving the
            # element with its fully-qualified namespace.
            self._pull_messages_type = self._pullpoint.zeep_client.get_element(
                f"{{{_EVENTS_NS}}}PullMessages"
            )
            self._subscribed_at = time.time()
            self._note("PullPoint aboneliği oluşturuldu.")
            self._set(subscribed=True)
            return True
        except Exception as e:
            self._pullpoint = None
            self._pull_messages_type = None
            self._fail(f"Abonelik oluşturulamadı: {e}", subscribed=False)
            return False

    # ------------------------------------------------------------------
    # ONVIF: event parsing (point 3)
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_topic(msg) -> str:
        try:
            topic_obj = msg.Topic
            value = (topic_obj._value_1 if hasattr(topic_obj, "_value_1")
                     else str(topic_obj))
            return value or ""
        except Exception:
            return ""

    @staticmethod
    def _extract_simple_items(msg) -> List[Tuple[str, str]]:
        """Pull the SimpleItem (Name/Value) pairs out of an event's Data.

        ``msg.Message._value_1`` is a raw lxml Element that zeep could not
        parse (xs:any content model), so it is walked namespace-agnostically
        with lxml's own API.
        """
        try:
            content = msg.Message._value_1
            if not hasattr(content, "iter"):
                return []
            items: List[Tuple[str, str]] = []
            for el in content.iter():
                tag = el.tag
                localname = ET.QName(tag).localname if isinstance(tag, str) else str(tag)
                if localname == "SimpleItem":
                    items.append((el.get("Name"), el.get("Value")))
            return items
        except Exception:
            return []

    def _handle_message(self, msg, now: float):
        """Apply one notification message to the detection state machine.

        This is where a camera-reported STOP is honoured: the same topic
        carries IsMotion/IsPeople as an explicit boolean, so `false`
        closes the interval immediately instead of waiting out the
        silence timeout. Cameras that never send the stop edge are still
        covered by _check_timeouts().
        """
        raw_topic = self._extract_topic(msg)
        # raw_topic can legitimately be empty: this camera also emits
        # unrelated topics (e.g. TPSmartEvent) whose Topic._value_1 comes
        # back blank. Guard before the substring scan.
        if not raw_topic:
            return
        field = value_key = None
        for key, (fname, vkey) in _TOPIC_TO_FIELD.items():
            if key in raw_topic:
                field, value_key = fname, vkey
                break
        if field is None or field not in self.enabled_kinds:
            return                                  # untracked topic / disabled kind

        for name, value in self._extract_simple_items(msg):
            if name != value_key:
                continue
            if str(value).strip().lower() == "true":
                self._mark_active(field, now)
            else:
                self._close_kind(field, now, "kamera bildirdi")
            return

    # ------------------------------------------------------------------
    # Detection state machine
    # ------------------------------------------------------------------
    def _mark_active(self, kind: str, ts: float):
        with self._lock:
            was_active = self._state[f"{kind}_active"]
            self._state[f"{kind}_active"] = True
            self._state[f"last_{kind}_at"] = ts
            self._state["last_event_at"] = ts
            self._last_seen[kind] = ts
            det_id = self._open_ids.get(kind)
            last_write = self._last_db_write.get(kind, 0.0)
        if not was_active or det_id is None:
            new_id = self.storage.open_detection(self.cam_id, kind, ts)
            with self._lock:
                self._open_ids[kind] = new_id
                self._last_db_write[kind] = ts
            if not was_active:
                self._log_line(f"{KIND_LABEL[kind]} başladı")
                self._maybe_notify(kind, ts)
        elif ts - last_write >= EXTEND_WRITE_MIN_INTERVAL_SEC:
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

    def _maybe_notify(self, kind: str, ts: float):
        """Fires once per false->true edge (called only from the
        `not was_active` branch above — never on the periodic "still
        active" pokes). No check for "is detection enabled for this kind"
        is needed here: _handle_message already filters to
        self.enabled_kinds before anything reaches the state machine."""
        try:
            group_ids = self.cam.get("group_ids") or []
            if not group_ids:
                # No group = no notifications. Notification config lives
                # entirely on groups; an ungrouped camera has nothing to
                # inherit from.
                return
            groups = {g["id"]: g for g in self.config_store.get_groups()}
            # A camera notifies if ANY of its groups says "active now" —
            # membership in one silenced group never mutes another.
            if not any(_group_notify_active(groups[gid], ts)
                       for gid in group_ids if gid in groups):
                return
            self.storage.create_notification(self.cam_id, kind, ts)
        except Exception as e:
            log.debug("notify check failed for %s/%s: %s", self.cam_id, kind, e)

    def _check_timeouts(self, now: float):
        """Fallback for cameras that never send a stop edge. Closes the
        interval at `last_seen` — the last instant we had real evidence —
        not at `now`, so a late check never inflates the recorded
        duration."""
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

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------
    def _sleep_interruptible(self, seconds: float):
        self._stop.wait(seconds)

    def _run(self):
        while not self._stop.is_set():
            # Silence timeouts must keep being evaluated even while the
            # ONVIF side is broken. Without this, a kind that was active
            # when the camera dropped would stay "active" forever: the
            # connect/subscribe failure paths below never reach the pull
            # loop, so nothing else would ever close it.
            self._check_timeouts(time.time())

            if self._cam is None:
                if not self._connect():
                    self._sleep_interruptible(CONNECT_RETRY_SLEEP_SEC)
                    continue

            if self._pullpoint is None:
                if not self._create_pullpoint():
                    # A failed subscribe usually means the session is bad;
                    # drop the camera handle so the next lap reconnects.
                    self._cam = None
                    self._sleep_interruptible(CONNECT_RETRY_SLEEP_SEC)
                    continue

            self._listen_until_resubscribe_needed()

            # Returned => the subscription must be recreated. Keep this gap
            # short: it is dead time in which no event can be caught.
            self._pullpoint = None
            self._pull_messages_type = None
            self._set(subscribed=False)
            if not self._stop.is_set():
                self._sleep_interruptible(RESUBSCRIBE_GAP_SEC)

    def _listen_until_resubscribe_needed(self):
        """Pull events until something says the subscription should be
        rebuilt: too many errors, or simply old enough to distrust."""
        unexpected_errors = 0
        benign_errors = 0

        while not self._stop.is_set():
            # Evaluated at the TOP of every lap, deliberately outside the
            # try below: a pull that raises is exactly when a stuck
            # "active" kind most needs closing, so this must not depend on
            # the pull having succeeded.
            now = time.time()
            self._check_timeouts(now)

            # Proactive renewal (point 6): even a subscription that has
            # never errored may have quietly stopped delivering.
            age = now - self._subscribed_at
            if age >= MAX_SUBSCRIPTION_AGE_SEC:
                self._note(f"Abonelik {age:.0f} saniyedir açık, proaktif olarak yenileniyor.")
                return

            try:
                req = self._pull_messages_type(
                    Timeout=timedelta(seconds=PULL_TIMEOUT_SEC),
                    MessageLimit=PULL_MESSAGE_LIMIT,
                )
                response = self._pullpoint.PullMessages(req)
                unexpected_errors = 0
                benign_errors = 0

                if response and response.NotificationMessage:
                    # Re-read the clock: a pull can block for
                    # PULL_TIMEOUT_SEC, so `now` from the top of the lap
                    # is stale by the time messages actually arrive.
                    arrived = time.time()
                    for msg in response.NotificationMessage:
                        # One malformed/unexpected message must never abort
                        # the rest of the batch or trip the error counters.
                        try:
                            self._handle_message(msg, arrived)
                        except Exception as msg_err:
                            log.debug("[%s] mesaj işlenemedi (atlandı): %s",
                                      self.cam_id, msg_err)

            except Exception as e:
                err = str(e)

                # Point 4: known-benign chatter. Retry quietly on a
                # separate, far more tolerant counter.
                if "RemoteDisconnected" in err or "Connection aborted" in err:
                    benign_errors += 1
                    if benign_errors >= MAX_BENIGN_ERRORS:
                        self._note(f"{benign_errors} ardışık bağlantı hatası, "
                                   f"abonelik yenileniyor.", logging.WARNING)
                        return
                    time.sleep(ERROR_RETRY_SLEEP_SEC)
                    continue

                # Genuinely unexpected. The exception TYPE is logged too
                # because ONVIFError's own message can be uselessly terse
                # (e.g. "Unknown error: error" — that is the camera's
                # entire SOAP Fault text); the type at least says which
                # layer failed (zeep Fault vs requests connection error).
                unexpected_errors += 1
                self._fail(f"Beklenmeyen pull hatası: {type(e).__name__}: {e}")
                if unexpected_errors >= MAX_UNEXPECTED_ERRORS:
                    self._note(f"{unexpected_errors} ardışık beklenmeyen hata, "
                               f"abonelik yenileniyor.", logging.WARNING)
                    return
                time.sleep(ERROR_RETRY_SLEEP_SEC)


class DetectionManager:
    """Starts/stops one CameraEventWatcher per opted-in camera and keeps
    them in sync with config changes (mirrors RecordingManager's
    supervisor pattern)."""

    SUPERVISOR_INTERVAL = 5

    def __init__(self, config_store, storage):
        self.cfg = config_store
        self.storage = storage
        self._watchers: Dict[str, CameraEventWatcher] = {}
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
        return bool(cam.get("onvif_host")) and bool(
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

    def status(self, cam_id: Optional[str] = None) -> dict:
        """Status keyed by camera id. Pass cam_id to get just that one:
        each entry carries up to LOG_MAXLEN debug lines, and the settings
        UI polls this every few seconds while showing exactly one camera —
        so returning all of them is mostly payload the caller discards."""
        with self._lock:
            watchers = dict(self._watchers)
        out = {}
        for c in self.cfg.get_cameras():
            if cam_id is not None and c["id"] != cam_id:
                continue
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
                    "timeout_seconds": int(c.get("motion_timeout_seconds",
                                                 MOTION_TIMEOUT_DEFAULT_SEC)
                                           or MOTION_TIMEOUT_DEFAULT_SEC),
                    "log": [],
                }
        return out

    def test_connection(self, cam: dict) -> dict:
        """Synchronous one-off connectivity probe for the UI's 'Test'
        button. Deliberately independent of the watcher above (which is
        long-running and asynchronous) and bounded by short timeouts so
        an unreachable camera fails fast instead of hanging the request
        thread."""
        if not ONVIF_AVAILABLE:
            return {"ok": False, "error": f"ONVIF kütüphanesi kurulu değil ({ONVIF_IMPORT_ERROR})"}
        host = cam.get("onvif_host")
        if not host:
            return {"ok": False, "error": "ONVIF host tanımlı değil"}
        try:
            port = int(cam.get("onvif_port", 80) or 80)
            kwargs = {}
            if Transport is not None:
                kwargs["transport"] = Transport(timeout=PROBE_WSDL_TIMEOUT_SEC,
                                                operation_timeout=PROBE_OPERATION_TIMEOUT_SEC)
            if ONVIF_WSDL_DIR:
                kwargs["wsdl_dir"] = ONVIF_WSDL_DIR
            onvif_cam = ONVIFCamera(host, port, cam.get("onvif_user", ""),
                                    cam.get("onvif_pass", ""), **kwargs)
            info = onvif_cam.create_devicemgmt_service().GetDeviceInformation()
            return {"ok": True,
                    "device": f"{info.Manufacturer} {info.Model} (fw {info.FirmwareVersion})"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
