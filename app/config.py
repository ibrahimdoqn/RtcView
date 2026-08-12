import json
import os
import threading
from pathlib import Path

DEFAULT_CONFIG = {
    "app": {
        "port": 5000,
        "host": "0.0.0.0",
        "grid_columns": 3,
        "theme": "dark",
        "show_camera_names": True,
        "show_status_badges": True,
        "auto_reconnect": True,
        "reconnect_delay_ms": 3000,
        # Linux sysfs thermal zone to read for the Sistem tab's CPU
        # temperature reading, in millidegrees C (the standard format for
        # every /sys/class/thermal/thermal_zone*/temp file). The zone
        # number that actually reports the SoC/CPU varies by board (RK3399
        # boards like the NanoPi R4S use zone0 for "cpu-thermal", but a Pi
        # or an x86 box may number it differently, or not expose one at
        # all) — user-editable rather than hardcoded so this works on
        # "any device", not just the one this was written against. Empty
        # string disables the reading entirely.
        "temp_sensor_path": "/sys/class/thermal/thermal_zone0/temp",
    },
    "go2rtc": {
        "host": "127.0.0.1",
        "api_port": 1984,
        "rtsp_port": 8554,
    },
    # Single Home Assistant instance RtcView pulls motion/person/vehicle
    # detection state from, over its WebSocket API. token is a long-lived
    # access token generated in HA's own user profile page. Stored in
    # plain text in config.json, same trust model as onvif_pass/tapo
    # fields elsewhere in this file — this app has no secrets vault.
    "home_assistant": {
        "url": "",
        "token": "",
        "verify_ssl": True,
    },
    "recording": {
        "enabled": True,
        # Single recording root. Deliberately ONE disk, mounted by the
        # admin outside the app (see README's Kurulum) — RtcView never
        # formats, mounts, or manages disks itself. Point this at
        # wherever that disk is mounted.
        "storage_path": "/opt/rtcview/recordings",
        "segment_seconds": 300,
        "retention_days": 14,
        # 0 = unlimited (bounded only by physical disk size + the
        # near-full rolling purge in storage.py).
        "max_gb": 0,
        "purge_interval_seconds": 60,
        "ffmpeg_path": "ffmpeg",
        # A healthy stream-copy ffmpeg process (this app never re-encodes
        # video) sits at 50-90 MB RSS indefinitely in normal operation.
        # If a recorder's ffmpeg grows past this ceiling, the supervisor
        # restarts it rather than let a leak run toward OOM. Tune down on
        # RAM-constrained boards (SBCs) where headroom across several
        # concurrent camera recorders matters; tune up if you'd rather
        # tolerate bigger legitimate bumps before restarting.
        "mem_rss_ceiling_mb": 128,
    },
    "cameras": [],
    "groups": [],  # [{"id": "grp_xxxxxxxx", "name": "İç Mekan"}]
}

# Per-camera recording defaults merged when a camera is loaded. Live
# transport is a per-device (localStorage) preference on the client — no
# server-side field for it.
CAMERA_RECORDING_DEFAULTS = {
    "record_mode": "off",          # off | always | schedule | manual
    "record_schedule": [],         # list of {"days":[0..6], "start":"HH:MM", "end":"HH:MM"}
    "record_audio": False,
    "retention_days_override": 0,  # 0 = use global
}

# Per-camera Home Assistant detection-source mapping. Each field is the
# entity_id of a binary_sensor in HA (e.g. "binary_sensor.front_door_
# motion") whose on/off state drives that camera's timeline/notifications
# for that kind — empty string means "not wired up, ignore this kind for
# this camera". Replaces the old ONVIF PullPoint-based motion/person
# detection (removed entirely — RtcView no longer talks ONVIF events to
# cameras itself, HA does that job and RtcView just consumes its state).
CAMERA_DETECTION_DEFAULTS = {
    "ha_motion_entity": "",
    "ha_person_entity": "",
    "ha_vehicle_entity": "",
}

# Camera keys the old ONVIF-based detection engine used that nothing
# reads any more. Stripped on load so a stale value can't sit in
# config.json looking meaningful. onvif_host/port/user/pass are NOT here
# — PTZ (app/ptz.py) still uses those independently of detection.
CAMERA_DETECTION_LEGACY_KEYS = (
    "motion_detection_enabled", "person_detection_enabled", "motion_timeout_seconds",
)

# Per-GROUP notification defaults (notifications are configured per group,
# not per camera — a camera inherits from every group it belongs to, and a
# camera in no group gets no notifications). notify_schedule uses the same
# shape as record_schedule, but an EMPTY schedule means the OPPOSITE thing
# here: "no time restriction" (notify any time) rather than record_
# schedule's "never" — a freshly-created group should just notify
# immediately, not require the user to configure a schedule first.
# notify_enabled is the single manual on/off switch (rendered as a toggle
# in the sidebar and in Ayarlar → Bildirimler). It replaced an earlier
# pair of timed overrides (notify_snooze_until / notify_force_until):
# those required picking an expiry time for every action, which was more
# ceremony than "silence this group" ever needed. Legacy keys are stripped
# on load — see _merge_defaults.
GROUP_NOTIFICATION_DEFAULTS = {
    "notify_enabled": True,
    "notify_schedule": [],
    # Epoch of the rule occurrence last acted on. Lets apply_notify_rules
    # tell "a boundary just passed" from "the user flipped the switch by
    # hand since then", so a manual override is never fought — and lets a
    # boundary missed during downtime still be applied at startup.
    "notify_rule_applied_at": 0,
}

# Group keys that older versions wrote and nothing reads any more. Removed
# on load so a stale value can't sit in config.json looking meaningful.
GROUP_LEGACY_KEYS = ("notify_snooze_until", "notify_force_until")


def _migrate_notify_schedule(schedule):
    """Convert a legacy notify_schedule from time WINDOWS to time ACTIONS.

    Old shape (same as record_schedule): {"days", "start", "end"} — a
    period during which notifications are on.
    New shape: {"days", "time", "action"} — a moment at which they are
    switched on or off. See apply_notify_rules in app/notify_rules.py.

    A NON-wrapping window (end > start) converts exactly: ON at `start`,
    OFF at `end`, same days.

    A wrapping window (end <= start, e.g. 20:00-09:00) needs three
    actions, because the old evaluator did NOT read it as "Monday evening
    through Tuesday morning". Its test was `dow in days AND (hm >= start
    OR hm < end)`, i.e. for each listed day it covered that day's EARLY
    MORNING *and* that day's evening. Reproducing that means: ON at
    00:00, OFF at `end`, ON at `start` — all on the same days.

    Caveat, deliberately accepted: on a day NOT in `days` that follows one
    that is, the old model went quiet at midnight whereas the action model
    simply carries the last state forward. Emitting a synthetic midnight
    OFF for every such day would bloat the list the user is about to read
    and edit, so the conversion is a faithful-but-simple starting point.
    """
    out = []
    for w in schedule or []:
        if not isinstance(w, dict):
            continue
        if "time" in w or "action" in w:
            out.append(w)                     # already migrated
            continue
        if "start" not in w and "end" not in w:
            continue                          # unrecognisable — drop it
        days = sorted({int(d) for d in (w.get("days") or [])
                       if isinstance(d, (int, float))})
        start = str(w.get("start") or "00:00")
        end = str(w.get("end") or "23:59")
        if start == end:
            # Degenerate "all day" window — just switch on and never off.
            out.append({"days": days, "time": "00:00", "action": "on"})
            continue
        if end > start:                       # "HH:MM" strings compare correctly
            out.append({"days": days, "time": start, "action": "on"})
            out.append({"days": days, "time": end, "action": "off"})
        else:
            if end != "00:00":
                out.append({"days": days, "time": "00:00", "action": "on"})
                out.append({"days": days, "time": end, "action": "off"})
            out.append({"days": days, "time": start, "action": "on"})
    return out

_lock = threading.Lock()


class ConfigStore:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # An interrupted _save_locked() leaves a `.tmp` sibling — sweep any
        # older than 5 minutes so the config dir doesn't accumulate cruft.
        try:
            import time as _t
            cutoff = _t.time() - 300
            for tmp in self.config_path.parent.glob("*.tmp"):
                try:
                    if tmp.stat().st_mtime < cutoff:
                        tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass
        self._data = None
        self.load()

    def load(self):
        with _lock:
            if self.config_path.exists():
                try:
                    with self.config_path.open("r", encoding="utf-8") as f:
                        self._data = json.load(f)
                except Exception:
                    self._data = json.loads(json.dumps(DEFAULT_CONFIG))
            else:
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
                self._save_locked()
            self._merge_defaults()

    def _merge_defaults(self):
        for k, v in DEFAULT_CONFIG.items():
            if k not in self._data:
                self._data[k] = json.loads(json.dumps(v))
            elif isinstance(v, dict):
                for sk, sv in v.items():
                    self._data[k].setdefault(sk, sv)
        # storage_path schema evolution: an earlier version supported
        # multiple storage roots via recording.storage_paths (a list of
        # {"path", "max_gb"} — or, before that, of bare strings). RtcView
        # is single-disk only now — collapse any of those old shapes down
        # to the one path a pre-existing config.json might still carry.
        # The disk that path pointed at keeps working exactly as before;
        # only the "add another folder" capability is gone.
        rec = self._data.get("recording", {})
        legacy_paths = rec.pop("storage_paths", None)
        # legacy_paths, when present, always wins over whatever the
        # defaults-merge loop above just filled storage_path with — an old
        # config never had storage_path set at all, only storage_paths.
        if legacy_paths:
            first = legacy_paths[0] if legacy_paths else None
            if isinstance(first, dict) and first.get("path"):
                rec["storage_path"] = str(first["path"])
            elif isinstance(first, str) and first:
                rec["storage_path"] = first
        rec.setdefault("storage_path", "/opt/rtcview/recordings")
        self._data.pop("disks", None)
        for cam in self._data.get("cameras", []):
            for k, v in CAMERA_RECORDING_DEFAULTS.items():
                cam.setdefault(k, json.loads(json.dumps(v)))
            for k, v in CAMERA_DETECTION_DEFAULTS.items():
                cam.setdefault(k, json.loads(json.dumps(v)))
            for k in CAMERA_DETECTION_LEGACY_KEYS:
                cam.pop(k, None)
            cam.setdefault("group_ids", [])
        for g in self._data.get("groups", []):
            for k, v in GROUP_NOTIFICATION_DEFAULTS.items():
                g.setdefault(k, json.loads(json.dumps(v)))
            for k in GROUP_LEGACY_KEYS:
                g.pop(k, None)
            g["notify_schedule"] = _migrate_notify_schedule(g.get("notify_schedule"))

    def _save_locked(self):
        tmp = self.config_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.config_path)

    def save(self):
        with _lock:
            self._save_locked()

    @property
    def data(self):
        return self._data

    def get_app(self):
        return self._data["app"]

    def get_go2rtc(self):
        return self._data["go2rtc"]

    def update_go2rtc(self, updates: dict):
        with _lock:
            self._data["go2rtc"].update(updates)
            self._save_locked()

    def get_home_assistant(self):
        return self._data["home_assistant"]

    def update_home_assistant(self, updates: dict):
        with _lock:
            self._data["home_assistant"].update(updates)
            self._save_locked()

    def get_recording(self):
        return self._data["recording"]

    def update_recording(self, updates: dict):
        with _lock:
            self._data["recording"].update(updates)
            self._save_locked()

    def get_groups(self):
        return self._data["groups"]

    def add_group(self, group: dict):
        with _lock:
            for k, v in GROUP_NOTIFICATION_DEFAULTS.items():
                group.setdefault(k, json.loads(json.dumps(v)))
            self._data["groups"].append(group)
            self._save_locked()

    def update_group(self, group_id: str, updates: dict) -> bool:
        with _lock:
            for g in self._data["groups"]:
                if g["id"] == group_id:
                    g.update(updates)
                    self._save_locked()
                    return True
            return False

    def remove_group(self, group_id: str) -> bool:
        with _lock:
            before = len(self._data["groups"])
            self._data["groups"] = [g for g in self._data["groups"] if g["id"] != group_id]
            removed = len(self._data["groups"]) != before
            if removed:
                for cam in self._data["cameras"]:
                    gids = cam.get("group_ids") or []
                    if group_id in gids:
                        cam["group_ids"] = [g for g in gids if g != group_id]
                self._save_locked()
            return removed

    def get_cameras(self):
        return self._data["cameras"]

    def set_cameras(self, cameras):
        with _lock:
            self._data["cameras"] = cameras
            self._save_locked()

    def update_app(self, updates: dict):
        with _lock:
            self._data["app"].update(updates)
            self._save_locked()

    def add_camera(self, camera: dict):
        with _lock:
            for k, v in CAMERA_RECORDING_DEFAULTS.items():
                camera.setdefault(k, json.loads(json.dumps(v)))
            for k, v in CAMERA_DETECTION_DEFAULTS.items():
                camera.setdefault(k, json.loads(json.dumps(v)))
            camera.setdefault("group_ids", [])
            self._data["cameras"].append(camera)
            self._save_locked()

    def update_camera(self, camera_id: str, updates: dict):
        with _lock:
            for cam in self._data["cameras"]:
                if cam["id"] == camera_id:
                    cam.update(updates)
                    self._save_locked()
                    return True
            return False

    def remove_camera(self, camera_id: str):
        with _lock:
            before = len(self._data["cameras"])
            self._data["cameras"] = [c for c in self._data["cameras"] if c["id"] != camera_id]
            if len(self._data["cameras"]) != before:
                self._save_locked()
                return True
            return False

    def reorder_cameras(self, ordered_ids):
        with _lock:
            id_map = {c["id"]: c for c in self._data["cameras"]}
            new_list = [id_map[i] for i in ordered_ids if i in id_map]
            for c in self._data["cameras"]:
                if c["id"] not in ordered_ids:
                    new_list.append(c)
            self._data["cameras"] = new_list
            self._save_locked()
