import os
import shutil
import subprocess
import time
import yaml
import requests
import threading
import logging
from pathlib import Path

log = logging.getLogger("go2rtc")


class Go2RtcManager:
    def __init__(self, config_store, install_dir: str):
        self.config_store = config_store
        self.install_dir = Path(install_dir)
        self.config_file = self.install_dir / "go2rtc.yaml"
        self.binary = shutil.which("go2rtc") or str(self.install_dir / "go2rtc")
        self.proc = None
        self._lock = threading.Lock()

    def _build_yaml(self):
        gc = self.config_store.get_go2rtc()
        cams = self.config_store.get_cameras()
        streams = {}
        for cam in cams:
            src = cam.get("stream_url", "").strip()
            if not src:
                continue
            streams[cam["id"]] = [src]

        doc = {
            "api": {"listen": f":{gc['api_port']}"},
            "rtsp": {"listen": ":8554"},
            "webrtc": {"listen": f":{gc['webrtc_port']}"},
            "log": {"level": "warn"},
            "streams": streams,
        }
        return doc

    def write_config(self):
        doc = self._build_yaml()
        self.install_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_file.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp, self.config_file)

    def is_running(self) -> bool:
        gc = self.config_store.get_go2rtc()
        try:
            r = requests.get(f"http://{gc['host']}:{gc['api_port']}/api", timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    def start(self):
        with self._lock:
            if self.is_running():
                log.info("go2rtc already running")
                return True
            if not os.path.exists(self.binary):
                log.warning("go2rtc binary not found at %s", self.binary)
                return False
            self.write_config()
            log.info("Starting go2rtc: %s -config %s", self.binary, self.config_file)
            try:
                self.proc = subprocess.Popen(
                    [self.binary, "-config", str(self.config_file)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=str(self.install_dir),
                )
            except Exception as e:
                log.error("Failed to start go2rtc: %s", e)
                return False
            for _ in range(30):
                if self.is_running():
                    return True
                time.sleep(0.2)
            return False

    def stop(self):
        with self._lock:
            if self.proc:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self.proc = None

    def reload(self):
        self.write_config()
        gc = self.config_store.get_go2rtc()
        try:
            requests.post(f"http://{gc['host']}:{gc['api_port']}/api/restart", timeout=2)
        except Exception:
            self.stop()
            self.start()
