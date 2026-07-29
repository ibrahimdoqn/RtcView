import logging
import os
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

# onvif-zeep's WSDL files are a data directory shipped next to (not
# inside) the installed `onvif` package, and that placement is known to
# go missing depending on pip version/install method. Prefer the
# tapo_detector package's own bundled copy (same repo, same onvif-zeep
# version) over trusting wherever pip happened to put onvif-zeep's.
_WSDL_DIR: Optional[str] = None
try:
    import tapo_detector as _tapo_detector
    _WSDL_DIR = os.path.join(os.path.dirname(os.path.abspath(_tapo_detector.__file__)), "wsdl")
    if not os.path.isdir(_WSDL_DIR):
        _WSDL_DIR = None
except Exception:
    _WSDL_DIR = None


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
            kwargs = {"wsdl_dir": _WSDL_DIR} if _WSDL_DIR else {}
            onvif = ONVIFCamera(host, port, user, pw, **kwargs)
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
