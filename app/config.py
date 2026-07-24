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
    },
    "cameras": []
}

_lock = threading.Lock()


class ConfigStore:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
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
