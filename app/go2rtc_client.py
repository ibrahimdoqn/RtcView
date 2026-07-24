import logging
import requests

log = logging.getLogger("go2rtc")


class Go2RtcClient:
    """Read-only client for an already-running go2rtc instance."""

    def __init__(self, config_store):
        self.config_store = config_store

    def base_url(self) -> str:
        gc = self.config_store.get_go2rtc()
        return f"http://{gc['host']}:{gc['api_port']}"

    def is_running(self) -> bool:
        try:
            r = requests.get(self.base_url() + "/api", timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    def list_streams(self):
        """Return list of stream names configured in go2rtc."""
        try:
            r = requests.get(self.base_url() + "/api/streams", timeout=3)
            if r.status_code != 200:
                return []
            data = r.json() or {}
            return sorted(data.keys()) if isinstance(data, dict) else []
        except Exception as e:
            log.debug("list_streams failed: %s", e)
            return []
