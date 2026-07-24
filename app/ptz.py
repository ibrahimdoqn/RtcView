import logging
import threading
from typing import Optional

log = logging.getLogger("ptz")

ONVIF_IMPORT_ERROR = None
try:
    from onvif import ONVIFCamera
    ONVIF_AVAILABLE = True
except Exception as _e:
    ONVIF_AVAILABLE = False
    ONVIF_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    log.warning("onvif-zeep import failed: %s", ONVIF_IMPORT_ERROR)


class PtzController:
    """Thin ONVIF PTZ wrapper. Cached per camera."""

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def _get(self, camera: dict):
        if not ONVIF_AVAILABLE:
            raise RuntimeError(
                "onvif-zeep kurulmamış. Çözüm: sudo /opt/rtcview/venv/bin/pip install onvif-zeep zeep "
                f"(import hatası: {ONVIF_IMPORT_ERROR})"
            )
        cam_id = camera["id"]
        with self._lock:
            if cam_id in self._cache:
                return self._cache[cam_id]
            host = camera.get("onvif_host") or camera.get("host")
            port = int(camera.get("onvif_port", 80))
            user = camera.get("onvif_user") or camera.get("username", "")
            pw = camera.get("onvif_pass") or camera.get("password", "")
            if not host:
                raise RuntimeError("Camera has no host configured for ONVIF")
            onvif = ONVIFCamera(host, port, user, pw)
            media = onvif.create_media_service()
            ptz = onvif.create_ptz_service()
            profiles = media.GetProfiles()
            if not profiles:
                raise RuntimeError("No media profiles found on camera")
            profile = profiles[0]
            entry = {"onvif": onvif, "ptz": ptz, "profile": profile, "token": profile.token}
            self._cache[cam_id] = entry
            return entry

    def move(self, camera: dict, pan: float, tilt: float, zoom: float, timeout: float = 0.6):
        entry = self._get(camera)
        req = entry["ptz"].create_type("ContinuousMove")
        req.ProfileToken = entry["token"]
        req.Velocity = {
            "PanTilt": {"x": float(pan), "y": float(tilt)},
            "Zoom": {"x": float(zoom)},
        }
        entry["ptz"].ContinuousMove(req)
        if timeout > 0:
            t = threading.Timer(timeout, lambda: self._safe_stop(entry))
            t.daemon = True
            t.start()

    def stop(self, camera: dict):
        entry = self._get(camera)
        self._safe_stop(entry)

    def _safe_stop(self, entry):
        try:
            req = entry["ptz"].create_type("Stop")
            req.ProfileToken = entry["token"]
            req.PanTilt = True
            req.Zoom = True
            entry["ptz"].Stop(req)
        except Exception as e:
            log.debug("PTZ stop error: %s", e)

    def goto_preset(self, camera: dict, preset_token: str):
        entry = self._get(camera)
        req = entry["ptz"].create_type("GotoPreset")
        req.ProfileToken = entry["token"]
        req.PresetToken = preset_token
        entry["ptz"].GotoPreset(req)

    def get_presets(self, camera: dict):
        entry = self._get(camera)
        presets = entry["ptz"].GetPresets({"ProfileToken": entry["token"]})
        return [{"token": p.token, "name": getattr(p, "Name", "")} for p in presets or []]

    def invalidate(self, camera_id: str):
        with self._lock:
            self._cache.pop(camera_id, None)


ptz_controller = PtzController()
