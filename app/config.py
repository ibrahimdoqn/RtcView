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
    },
    "go2rtc": {
        "host": "127.0.0.1",
        "api_port": 1984,
        "rtsp_port": 8554,
    },
    "recording": {
        "enabled": True,
        # List of storage roots, each with ITS OWN quota. New segments go
        # to whichever writable root has the most free space at start
        # time (skipping any that have hit their own quota); playback +
        # purge span every root. Each entry: {"path": str, "max_gb": int}
        # where max_gb=0 means unlimited for that specific disk.
        "storage_paths": [{"path": "/opt/rtcview/recordings", "max_gb": 0}],
        "segment_seconds": 300,
        "retention_days": 14,
        # Legacy global quota — kept for backward compat only. Used as
        # the default for freshly added roots that don't set max_gb.
        "max_gb": 0,
        "purge_interval_seconds": 60,
        "ffmpeg_path": "ffmpeg",
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

# Per-camera ONVIF motion/person event-detection defaults. Opt-in per
# camera since not every camera's ONVIF stack actually emits these
# topics. motion_timeout_seconds handles cameras (e.g. Tapo) that only
# ever send the "started" edge and never report motion stopping: if no
# new event for a kind arrives within this window, it's treated as over.
CAMERA_DETECTION_DEFAULTS = {
    "motion_detection_enabled": False,
    "person_detection_enabled": False,
    "motion_timeout_seconds": 15,
}

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
}

# Group keys that older versions wrote and nothing reads any more. Removed
# on load so a stale value can't sit in config.json looking meaningful.
GROUP_LEGACY_KEYS = ("notify_snooze_until", "notify_force_until")


def _migrate_notify_schedule(schedule):
    """Convert a legacy notify_schedule from time WINDOWS to time ACTIONS.

    Old shape (same as record_schedule): {"days", "start", "end"} — a
    period during which notifications are on.
    New shape: {"days", "time", "action"} — a moment at which they are
    switched on or off. See _notify_rules_active in detection.py.

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
        # storage_paths schema evolution:
        #  1) legacy scalar recording.storage_path (very old)
        #  2) list of strings  ["/mnt/a", "/mnt/b"]
        #  3) list of objects  [{"path": "/mnt/a", "max_gb": 500}]  ← current
        # Migrate 1→2 and 2→3 in place. The legacy global recording.max_gb
        # is used as the default per-disk quota when upgrading from #2.
        rec = self._data.get("recording", {})
        legacy = rec.get("storage_path")
        paths = rec.get("storage_paths") or []
        if legacy and not paths:
            rec["storage_paths"] = [legacy]
            paths = rec["storage_paths"]
        default_quota = int(rec.get("max_gb", 0) or 0)
        migrated = []
        for entry in paths:
            if isinstance(entry, str):
                migrated.append({"path": entry, "max_gb": default_quota})
            elif isinstance(entry, dict) and entry.get("path"):
                migrated.append({
                    "path": str(entry["path"]),
                    "max_gb": int(entry.get("max_gb", default_quota) or 0),
                })
        rec["storage_paths"] = migrated or [{"path": "/opt/rtcview/recordings", "max_gb": 0}]
        for cam in self._data.get("cameras", []):
            for k, v in CAMERA_RECORDING_DEFAULTS.items():
                cam.setdefault(k, json.loads(json.dumps(v)))
            for k, v in CAMERA_DETECTION_DEFAULTS.items():
                cam.setdefault(k, json.loads(json.dumps(v)))
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
