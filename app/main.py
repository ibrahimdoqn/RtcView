import argparse
import atexit
import logging
import mimetypes
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context
from flask_cors import CORS

from app.config import ConfigStore
from app.homeassistant import HAManager, WEBSOCKET_AVAILABLE as HA_WEBSOCKET_AVAILABLE
from app import homeassistant as ha_module
from app.go2rtc_client import Go2RtcClient
from app.netmon import NetworkMonitor
from app.ptz import ptz_controller, ONVIF_AVAILABLE
from app.recorder import RecordingManager, MANUAL_DEFAULT_SECONDS
from app.storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rtcview")


def resolve_paths():
    install_dir = os.environ.get("RTCVIEW_HOME", "/opt/rtcview")
    config_dir = os.environ.get("RTCVIEW_CONFIG", os.path.join(install_dir, "config"))
    return install_dir, config_dir


def _int_arg(args, name: str, default: int) -> int:
    """int(request.args.get(name, default)) without turning a malformed
    query string (?limit=abc) into an unhandled 500."""
    try:
        return int(args.get(name, default))
    except (TypeError, ValueError):
        return default


def create_app(config_path: str) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    # Cap request bodies at 2 MB so a stray/malicious POST can't OOM the
    # server. All real payloads (config, camera CRUD, recording settings)
    # are far below this.
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    CORS(app)

    # A camera id becomes a directory name under every storage root and a
    # dict key several subsystems index workers by, so it's validated
    # everywhere one can originate from user input, not just here. The
    # character class alone lets "." and ".." (both otherwise-legal
    # matches) through as literal path-traversal segments once handed to
    # Path.__truediv__ (root / cam_id / ...) — _valid_cam_id below closes
    # that off explicitly rather than relying on a cleverer regex.
    _CAM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

    def _valid_cam_id(cam_id) -> bool:
        return bool(cam_id) and bool(_CAM_ID_RE.match(cam_id)) and cam_id not in (".", "..")

    _HA_ENTITY_RE = re.compile(r"^binary_sensor\.[a-z0-9_]+$")
    _HA_NOTIFY_ENTITY_RE = re.compile(r"^input_boolean\.[a-z0-9_]+$")
    _RECORD_MODES = ("off", "always", "schedule", "manual")

    def _validate_ha_entities(body):
        """Pull ha_{motion,person,vehicle}_entity out of a camera request
        body. Empty string means "not wired up" (valid — a camera can
        leave any/all of these blank). A non-empty value must be a real
        binary_sensor.* entity_id (the only domain the sensor picker ever
        offers — see app/homeassistant.py's fetch_entities) so a typo or a
        pasted friendly-name doesn't silently sit there matching nothing
        forever. Returns (dict_with_all_three_keys, None) or (None, error).
        """
        out = {}
        for kind in ("motion", "person", "vehicle"):
            key = f"ha_{kind}_entity"
            val = str(body.get(key) or "").strip()
            if val and not _HA_ENTITY_RE.match(val):
                return None, f"invalid {key} — must be a binary_sensor.* entity id"
            out[key] = val
        return out, None

    def _validate_int_field(body, key, default=0):
        """Coerce body[key] to int, defaulting when the key is absent or
        blank. Returns (value, None) or (None, error). Camera fields like
        onvif_port/retention_days_override used to go straight through
        int(...) with no try/except on POST (a non-numeric value crashed
        with an uncaught ValueError -> 500 instead of a clean 400) and
        with no coercion at all on PUT (a partial update just stores
        whatever type the client sent, e.g. a string "abc", which then
        breaks later when ptz.py does int(camera["onvif_port"]))."""
        if key not in body:
            return default, None
        raw = body.get(key)
        if raw is None or raw == "":
            return default, None
        try:
            return int(raw), None
        except (TypeError, ValueError):
            return None, f"invalid {key}"

    def _validate_record_schedule(raw):
        """Normalize/validate a record_schedule value into the window shape
        recorder.py's _schedule_active expects: a list of
        {"days":[0..6], "start":"HH:MM", "end":"HH:MM"}. Returns
        (clean_list, None) on success or (None, error_message) on failure.

        _schedule_active itself only guards the start/end PARSING of an
        individual row (try/except around the split+int), not the shape of
        `schedule` as a whole or of each row — if record_schedule is ever
        anything other than a list of dicts (e.g. a stray string from a
        malformed client), `for w in schedule: w.get("days")` raises
        AttributeError iterating a string's characters. That exception
        wasn't caught per-camera in RecordingManager._tick()'s loop, so it
        escaped to the loop's caller and skipped every camera after the
        broken one, every 3s, until the config was fixed. Rejecting a bad
        shape here — at the one place record_schedule can be written —
        means _schedule_active never has to see it.
        """
        if raw is None:
            return [], None
        if not isinstance(raw, list):
            return None, "invalid record_schedule"
        clean = []
        for w in raw:
            if not isinstance(w, dict):
                return None, "invalid record_schedule row"
            days = []
            for d in (w.get("days") or []):
                try:
                    d = int(d)
                except (TypeError, ValueError):
                    return None, "invalid record_schedule day"
                if not 0 <= d <= 6:
                    return None, "invalid record_schedule day"
                days.append(d)

            def _parse_hm(v, default):
                try:
                    hh, mm = (int(x) for x in str(v if v is not None else default).split(":", 1))
                except (TypeError, ValueError):
                    return None
                return f"{hh:02d}:{mm:02d}" if (0 <= hh <= 23 and 0 <= mm <= 59) else None

            start = _parse_hm(w.get("start"), "00:00")
            end = _parse_hm(w.get("end"), "23:59")
            if start is None or end is None:
                return None, "invalid record_schedule time"
            clean.append({"days": sorted(set(days)), "start": start, "end": end})
        return clean, None

    store = ConfigStore(config_path)
    go2rtc = Go2RtcClient(store)
    storage = Storage(store)
    recorder = RecordingManager(store, storage)
    ha_mgr = HAManager(store, storage)
    netmon = NetworkMonitor()

    app.config["STORE"] = store
    app.config["GO2RTC"] = go2rtc
    app.config["STORAGE"] = storage
    app.config["RECORDER"] = recorder
    app.config["HA_MANAGER"] = ha_mgr
    app.config["NETMON"] = netmon

    storage.start()
    recorder.start()
    ha_mgr.start()
    netmon.start()

    def _shutdown():
        try: netmon.stop()
        except Exception: pass
        try: ha_mgr.stop()
        except Exception: pass
        try: recorder.stop()
        except Exception: pass
        try: storage.stop()
        except Exception: pass
    atexit.register(_shutdown)

    # ---------- Pages ----------
    _asset_ver_cache = [0.0, "0"]      # [computed_at, value]
    def _asset_version():
        # Refreshing this on every page hit meant a full rglob over
        # static/ per request. 60 s cache is more than plenty.
        now = time.time()
        if now - _asset_ver_cache[0] < 60:
            return _asset_ver_cache[1]
        try:
            base = Path(app.static_folder)
            latest = max(
                (p.stat().st_mtime for p in base.rglob("*") if p.is_file()),
                default=0,
            )
            _asset_ver_cache[0] = now; _asset_ver_cache[1] = str(int(latest))
            return _asset_ver_cache[1]
        except Exception:
            return "0"

    @app.route("/")
    def index():
        return render_template("index.html", asset_version=_asset_version())

    @app.route("/manifest.webmanifest")
    def manifest():
        return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")

    @app.route("/sw.js")
    def sw():
        resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    # ---------- API: status / config ----------
    @app.get("/api/status")
    def api_status():
        gc = store.get_go2rtc()
        return jsonify({
            "go2rtc_running": go2rtc.is_running(),
            "go2rtc": gc,
            "onvif_available": ONVIF_AVAILABLE,
            "ha_websocket_available": HA_WEBSOCKET_AVAILABLE,
            "recording_enabled": store.get_recording().get("enabled", True),
            "version": "4.0.0",
        })

    @app.get("/api/config")
    def api_config():
        return jsonify(store.data)

    @app.get("/api/settings")
    def api_settings_get():
        return jsonify(store.get_app())

    @app.post("/api/settings")
    def api_settings_set():
        updates = request.get_json(force=True) or {}
        allowed = {"grid_columns", "theme", "show_camera_names",
                   "show_status_badges", "auto_reconnect", "reconnect_delay_ms",
                   "temp_sensor_path"}
        clean = {k: v for k, v in updates.items() if k in allowed}
        if "grid_columns" in clean:
            try:
                gc = int(clean["grid_columns"])
                if gc < 1 or gc > 8:
                    return jsonify({"error": "grid_columns must be 1-8"}), 400
                clean["grid_columns"] = gc
            except Exception:
                return jsonify({"error": "invalid grid_columns"}), 400
        if "temp_sensor_path" in clean:
            clean["temp_sensor_path"] = str(clean["temp_sensor_path"] or "").strip()
        store.update_app(clean)
        return jsonify(store.get_app())

    @app.get("/api/go2rtc/settings")
    def api_go2rtc_get():
        return jsonify(store.get_go2rtc())

    @app.post("/api/go2rtc/settings")
    def api_go2rtc_set():
        body = request.get_json(force=True) or {}
        allowed = {"host", "api_port", "rtsp_port"}
        clean = {k: v for k, v in body.items() if k in allowed}
        for key in ("api_port", "rtsp_port"):
            if key in clean:
                try: clean[key] = int(clean[key])
                except Exception: return jsonify({"error": f"invalid {key}"}), 400
        store.update_go2rtc(clean)
        recorder.reload_all()  # RTSP port could have changed
        return jsonify(store.get_go2rtc())

    @app.get("/api/go2rtc/streams")
    def api_go2rtc_streams():
        return jsonify(go2rtc.list_streams())

    # ---------- API: cameras ----------
    @app.get("/api/cameras")
    def api_cameras():
        return jsonify(store.get_cameras())

    @app.post("/api/cameras")
    def api_add_camera():
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        stream = (body.get("stream") or "").strip()
        if not name or not stream:
            return jsonify({"error": "name and stream are required"}), 400
        ha_entities, err = _validate_ha_entities(body)
        if err:
            return jsonify({"error": err}), 400
        # cam_id becomes a directory name under every storage root
        # (_cam_dir in recorder.py) and a dict key workers are indexed by —
        # an unvalidated id from the client is a path-traversal vector
        # ("../../etc") and a duplicate id would silently desync in-memory
        # recorder/detector state from the config list.
        cam_id = body.get("id") or "cam_" + uuid.uuid4().hex[:8]
        if not _valid_cam_id(cam_id):
            return jsonify({"error": "invalid camera id"}), 400
        if any(c["id"] == cam_id for c in store.get_cameras()):
            return jsonify({"error": "camera id already exists"}), 400
        record_schedule, err = _validate_record_schedule(body.get("record_schedule"))
        if err:
            return jsonify({"error": err}), 400
        onvif_port, err = _validate_int_field(body, "onvif_port", 80)
        if err:
            return jsonify({"error": err}), 400
        retention_days_override, err = _validate_int_field(body, "retention_days_override", 0)
        if err:
            return jsonify({"error": err}), 400
        record_mode = body.get("record_mode", "off")
        if record_mode not in _RECORD_MODES:
            return jsonify({"error": f"invalid record_mode — must be one of {_RECORD_MODES}"}), 400
        cam = {
            "id": cam_id,
            "name": name,
            "stream": stream,
            "ptz_enabled": bool(body.get("ptz_enabled", False)),
            "onvif_host": body.get("onvif_host", ""),
            "onvif_port": onvif_port,
            "onvif_user": body.get("onvif_user", ""),
            "onvif_pass": body.get("onvif_pass", ""),
            "record_mode": record_mode,
            "record_schedule": record_schedule,
            "record_audio": bool(body.get("record_audio", False)),
            "retention_days_override": retention_days_override,
            **ha_entities,
            "group_ids": [str(g) for g in (body.get("group_ids") or []) if str(g).strip()],
        }
        store.add_camera(cam)
        recorder.reload_camera(cam["id"])
        ha_mgr.reload_camera(cam["id"])
        return jsonify(cam), 201

    @app.put("/api/cameras/<camera_id>")
    def api_update_camera(camera_id):
        body = request.get_json(force=True) or {}
        # Legacy field cleanup: older configs stored stream_mode per camera;
        # transport is now a device-scoped localStorage preference, so drop
        # any incoming stream_mode value.
        body.pop("stream_mode", None)
        # The camera id is a path component in every stored segment/
        # snapshot filename and the key every subsystem (recorder,
        # detector, storage rows) indexes by. It's immutable after
        # creation — the frontend never sends it in a PUT body (only as
        # the URL route param), so unconditionally dropping it here closes
        # off what would otherwise be an unvalidated dict.update() straight
        # into cam["id"] (no regex check on this path, unlike POST), and
        # avoids orphaning that camera's existing DB rows/on-disk files
        # from a renamed id even if a caller did send a well-formed one.
        body.pop("id", None)
        if any(k in body for k in ("ha_motion_entity", "ha_person_entity", "ha_vehicle_entity")):
            ha_entities, err = _validate_ha_entities(body)
            if err:
                return jsonify({"error": err}), 400
            # Only overwrite the keys actually present in the request —
            # unlike POST /api/cameras (a full create), a PUT is a partial
            # update and must leave an omitted entity field untouched
            # rather than blanking it back to "".
            for k in ("ha_motion_entity", "ha_person_entity", "ha_vehicle_entity"):
                if k not in body:
                    ha_entities.pop(k, None)
            body.update(ha_entities)
        if "group_ids" in body:
            body["group_ids"] = [str(g) for g in (body.get("group_ids") or []) if str(g).strip()]
        if "record_schedule" in body:
            clean, err = _validate_record_schedule(body.get("record_schedule"))
            if err:
                return jsonify({"error": err}), 400
            body["record_schedule"] = clean
        if "record_mode" in body and body["record_mode"] not in _RECORD_MODES:
            return jsonify({"error": f"invalid record_mode — must be one of {_RECORD_MODES}"}), 400
        # PUT is a partial update whose body gets merged straight into the
        # stored camera dict (store.update_camera below) — unlike POST,
        # nothing here used to coerce these to int, so a malformed client
        # value (a stray string) landed in config.json as-is and only
        # broke later when ptz.py did int(camera["onvif_port"]).
        for key, default in (("onvif_port", 80), ("retention_days_override", 0)):
            if key in body:
                v, err = _validate_int_field(body, key, default)
                if err:
                    return jsonify({"error": err}), 400
                body[key] = v
        ok = store.update_camera(camera_id, body)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ptz_controller.invalidate(camera_id)
        recorder.reload_camera(camera_id)
        ha_mgr.reload_camera(camera_id)
        return jsonify({"ok": True})

    @app.delete("/api/cameras/<camera_id>")
    def api_delete_camera(camera_id):
        ok = store.remove_camera(camera_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ptz_controller.invalidate(camera_id)
        recorder.reload_camera(camera_id)
        ha_mgr.reload_camera(camera_id)
        return jsonify({"ok": True})

    @app.post("/api/cameras/reorder")
    def api_reorder():
        body = request.get_json(force=True) or {}
        order = body.get("order", [])
        if not isinstance(order, list):
            return jsonify({"error": "order must be a list"}), 400
        store.reorder_cameras(order)
        return jsonify({"ok": True})

    # ---------- Camera groups ----------
    def _with_notify_active(group: dict) -> dict:
        # notify_active is computed, never stored — it's just the live
        # on/off state of the group's ha_notify_entity, read straight off
        # HAManager's WebSocket connection (see HAManager.group_notify_active).
        out = dict(group)
        out["notify_active"] = ha_mgr.group_notify_active(group)
        return out

    @app.get("/api/groups")
    def api_groups_list():
        return jsonify([_with_notify_active(g) for g in store.get_groups()])

    @app.post("/api/groups")
    def api_groups_add():
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        group = {"id": "grp_" + uuid.uuid4().hex[:8], "name": name}
        store.add_group(group)
        ha_mgr.reload()
        return jsonify(_with_notify_active(group)), 201

    @app.put("/api/groups/<group_id>")
    def api_groups_update(group_id):
        body = request.get_json(force=True) or {}
        updates = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                return jsonify({"error": "name required"}), 400
            updates["name"] = name
        if "ha_notify_entity" in body:
            val = str(body.get("ha_notify_entity") or "").strip()
            if val and not _HA_NOTIFY_ENTITY_RE.match(val):
                return jsonify({"error": "invalid ha_notify_entity — must be an input_boolean.* entity id"}), 400
            updates["ha_notify_entity"] = val
        if not updates:
            return jsonify({"error": "no valid fields"}), 400
        ok = store.update_group(group_id, updates)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ha_mgr.reload()
        g = next((g for g in store.get_groups() if g["id"] == group_id), None)
        return jsonify(_with_notify_active(g) if g else {"ok": True})

    @app.delete("/api/groups/<group_id>")
    def api_groups_delete(group_id):
        ok = store.remove_group(group_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ha_mgr.reload()
        return jsonify({"ok": True})

    # ---------- PTZ ----------
    def _find_camera(camera_id):
        for c in store.get_cameras():
            if c["id"] == camera_id:
                return c
        return None

    # ---------- Motion / person / vehicle detection (Home Assistant) ----------
    @app.get("/api/detection/status")
    def api_detection_status():
        # Optional ?cam=<id> narrows the response to one camera. Each entry
        # carries a long debug log, so the UI (which shows one camera at a
        # time) asks for just that one; omitting the arg keeps the original
        # all-cameras behaviour.
        return jsonify(ha_mgr.status(cam_id=request.args.get("cam") or None))

    @app.get("/api/detection/events")
    def api_detection_events():
        cam = request.args.get("cam")
        try:
            t_from = float(request.args.get("from")) if request.args.get("from") else None
            t_to = float(request.args.get("to")) if request.args.get("to") else None
        except ValueError:
            return jsonify({"error": "invalid time range"}), 400
        limit = _int_arg(request.args, "limit", 5000)
        return jsonify(storage.list_detections(cam_id=cam, t_from=t_from, t_to=t_to, limit=limit))

    # ---------- Home Assistant integration ----------
    @app.get("/api/homeassistant/settings")
    def api_ha_settings_get():
        cfg = dict(store.get_home_assistant())
        # Never echo the access token back to the client — the settings
        # form only needs to know one exists (to show "•••" / a "kayıtlı"
        # state) and offer to replace it, not read the live value back.
        cfg["token_set"] = bool(cfg.pop("token", ""))
        return jsonify(cfg)

    @app.post("/api/homeassistant/settings")
    def api_ha_settings_set():
        body = request.get_json(force=True) or {}
        updates = {}
        if "url" in body:
            updates["url"] = str(body.get("url") or "").strip()
        if "token" in body:
            # Blank means "leave the stored token alone" — the form never
            # has the real value to send back after api_ha_settings_get's
            # redaction, so an empty submit must not wipe a working token.
            token = str(body.get("token") or "").strip()
            if token:
                updates["token"] = token
        if "verify_ssl" in body:
            updates["verify_ssl"] = bool(body.get("verify_ssl"))
        if updates:
            store.update_home_assistant(updates)
        cfg = dict(store.get_home_assistant())
        cfg["token_set"] = bool(cfg.pop("token", ""))
        return jsonify(cfg)

    @app.post("/api/homeassistant/test")
    def api_ha_test():
        cfg = store.get_home_assistant()
        return jsonify(ha_module.test_connection(
            cfg.get("url"), cfg.get("token"), cfg.get("verify_ssl", True)))

    _HA_ENTITY_DOMAINS = ("binary_sensor", "input_boolean")

    @app.get("/api/homeassistant/entities")
    def api_ha_entities():
        # binary_sensor for the camera detection pickers, input_boolean for
        # the group notify-gate picker — anything else is rejected rather
        # than silently defaulting, so a typo'd query param fails loudly.
        domain = request.args.get("domain", "binary_sensor")
        if domain not in _HA_ENTITY_DOMAINS:
            return jsonify({"error": f"invalid domain — must be one of {_HA_ENTITY_DOMAINS}"}), 400
        cfg = store.get_home_assistant()
        return jsonify(ha_module.fetch_entities(
            cfg.get("url"), cfg.get("token"), cfg.get("verify_ssl", True), domain=domain))

    # ---------- Notifications ----------
    # No global on/off or global snooze — notification config lives
    # entirely on groups now (PUT /api/groups/<id>, see above).
    @app.get("/api/notifications")
    def api_notifications_list():
        unread_only = request.args.get("unread_only") in ("1", "true", "yes")
        try:
            limit = max(1, min(500, int(request.args.get("limit", 200))))
        except ValueError:
            limit = 200
        return jsonify(storage.list_notifications(unread_only=unread_only, limit=limit))

    @app.post("/api/notifications/read-all")
    def api_notifications_read_all():
        storage.mark_all_notifications_read()
        return jsonify({"ok": True})

    @app.delete("/api/notifications")
    def api_notifications_clear():
        storage.clear_all_notifications()
        return jsonify({"ok": True})

    @app.post("/api/ptz/<camera_id>/move")
    def api_ptz_move(camera_id):
        cam = _find_camera(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        if not cam.get("ptz_enabled"):
            return jsonify({"error": "PTZ not enabled"}), 400
        body = request.get_json(force=True) or {}
        try:
            pan = float(body.get("pan", 0))
            tilt = float(body.get("tilt", 0))
            zoom = float(body.get("zoom", 0))
            timeout = float(body.get("timeout", 0.6))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid pan/tilt/zoom/timeout"}), 400
        try:
            ptz_controller.move(cam, pan, tilt, zoom, timeout=timeout)
            return jsonify({"ok": True})
        except Exception as e:
            log.warning("PTZ move failed: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.post("/api/ptz/<camera_id>/stop")
    def api_ptz_stop(camera_id):
        cam = _find_camera(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        try:
            ptz_controller.stop(cam)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get("/api/ptz/<camera_id>/presets")
    def api_ptz_presets(camera_id):
        cam = _find_camera(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        try:
            return jsonify(ptz_controller.get_presets(cam))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/ptz/<camera_id>/preset/<preset_token>")
    def api_ptz_goto(camera_id, preset_token):
        cam = _find_camera(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        try:
            ptz_controller.goto_preset(cam, preset_token)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ---------- Recording: settings, status ----------
    @app.get("/api/recording/settings")
    def api_rec_settings_get():
        return jsonify(store.get_recording())

    @app.post("/api/recording/settings")
    def api_rec_settings_set():
        body = request.get_json(force=True) or {}
        allowed = {"enabled", "storage_path", "segment_seconds",
                   "retention_days", "max_gb", "purge_interval_seconds", "ffmpeg_path",
                   "mem_rss_ceiling_mb"}
        clean = {k: v for k, v in body.items() if k in allowed}
        new_path = clean.pop("storage_path", None)
        # Validate simple integer / bool fields BEFORE mutating anything.
        if "segment_seconds" in clean:
            try:
                s = int(clean["segment_seconds"])
                if s < 30 or s > 3600: return jsonify({"error": "segment_seconds 30-3600"}), 400
                clean["segment_seconds"] = s
            except Exception: return jsonify({"error": "invalid segment_seconds"}), 400
        for key in ("retention_days", "max_gb", "purge_interval_seconds"):
            if key in clean:
                try: clean[key] = int(clean[key])
                except Exception: return jsonify({"error": f"invalid {key}"}), 400
        if "mem_rss_ceiling_mb" in clean:
            try:
                m = int(clean["mem_rss_ceiling_mb"])
                if m < 32 or m > 4096: return jsonify({"error": "mem_rss_ceiling_mb 32-4096"}), 400
                clean["mem_rss_ceiling_mb"] = m
            except Exception: return jsonify({"error": "invalid mem_rss_ceiling_mb"}), 400
        if "purge_interval_seconds" in clean:
            # The purge loop does self._stop.wait(purge_interval_seconds)
            # between full-table retention/quota scans; 0 (or negative)
            # makes it a busy loop pegging a core forever.
            clean["purge_interval_seconds"] = max(5, clean["purge_interval_seconds"])
        if "enabled" in clean: clean["enabled"] = bool(clean["enabled"])
        # Storage-path change is validated + applied FIRST, and everything
        # else in this request is only persisted once that succeeds — a
        # rejected path (unwritable, uncreatable) must leave every other
        # field in this same request untouched rather than saving them
        # alongside the 400, which used to happen because update_recording
        # ran on `clean` before set_root got a chance to reject the path.
        moving_root = False
        if new_path is not None:
            wanted = str(Path(str(new_path)).expanduser().resolve())
            moving_root = wanted != str(storage.root())
        if moving_root:
            recorder.stop()
        if new_path is not None:
            # set_root persists storage_path (and max_gb, if given here)
            # in one shot — pulled out of `clean` so a rejected path can
            # never leave max_gb saved without it.
            ok, msg = storage.set_root(new_path, max_gb=clean.pop("max_gb", None))
            if not ok:
                if moving_root: recorder.start()
                return jsonify({"error": msg}), 400
        if clean: store.update_recording(clean)
        if moving_root:
            recorder.start()
        else:
            recorder.reload_all()
        return jsonify(store.get_recording())

    @app.get("/api/recording/status")
    def api_rec_status():
        health = storage.health()
        # Surface any recent ffmpeg complaints that smell disk-related. Bumps
        # the health status if we're currently reporting "ok".
        disk_hints = ("no space", "disk full", "permission denied",
                      "read-only", "no such file or directory",
                      "input/output error", "device or resource busy")
        ffmpeg_disk_errors = []
        try:
            with recorder._lock:
                recs = list(recorder._recs.values())
            for r in recs:
                for line in list(r._stderr_tail)[-40:]:
                    ll = line.lower()
                    if any(k in ll for k in disk_hints):
                        ffmpeg_disk_errors.append({"cam_id": r.cam_id, "msg": line})
                        break
        except Exception:
            pass
        if ffmpeg_disk_errors and health["status"] == "ok":
            health["status"] = "warning"
        health["ffmpeg_disk_errors"] = ffmpeg_disk_errors
        return jsonify({
            "settings": store.get_recording(),
            "cameras": recorder.status(),
            "storage": storage.stats(),
            "health": health,
            "ffmpeg_available": bool(_which(store.get_recording().get("ffmpeg_path") or "ffmpeg")),
        })

    @app.post("/api/recording/rescan")
    def api_rec_rescan():
        return jsonify(storage.rescan())

    @app.post("/api/recording/purge")
    def api_rec_purge():
        return jsonify(storage.purge_once())

    # ---------- System stats + logs ----------
    @app.get("/api/system/stats")
    def api_system_stats():
        return jsonify(_read_system_stats(recorder, store))

    @app.get("/api/network/status")
    def api_network_status():
        return jsonify(netmon.snapshot())

    @app.get("/api/system/logs")
    def api_system_logs():
        try: lines = max(5, min(2000, int(request.args.get("lines", 200))))
        except ValueError: lines = 200
        level = (request.args.get("level") or "").lower()
        # journalctl priority: 0=emerg .. 7=debug; 4=warning, 3=err
        prio_arg = []
        if level == "err":  prio_arg = ["-p", "3"]
        elif level == "warn": prio_arg = ["-p", "4"]
        try:
            r = subprocess.run(
                ["journalctl", "-u", "rtcview.service", "-n", str(lines),
                 "--no-pager", "--output=short-iso"] + prio_arg,
                capture_output=True, text=True, timeout=6,
            )
            if r.returncode != 0:
                return jsonify({"ok": False, "error": (r.stderr or "").strip() or "journalctl failed",
                                "hint": "rtcview kullanıcısını systemd-journal grubuna ekleyin: sudo usermod -aG systemd-journal rtcview"}), 500
            return jsonify({"ok": True, "log": r.stdout, "lines": r.stdout.count("\n")})
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "journalctl bulunamadı (systemd yok?)"}), 500
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "error": "journalctl zaman aşımı"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ---------- Self-update (GitHub) ----------
    # The running app can't sudo out of itself — rtcview.service runs with
    # NoNewPrivileges=yes (deliberate hardening), which blocks any setuid-
    # based privilege escalation from this process, full stop. So instead
    # of asking for root, this just touches a trigger FILE inside the
    # app's own writable INSTALL_DIR — no privilege boundary crossed at
    # all. A separate systemd .path unit (root, its own unit/cgroup,
    # entirely unrelated to rtcview.service) notices the file and runs
    # self_update.sh; both units are (re)generated by install.sh/update.sh.
    @app.get("/api/system/update/status")
    def api_system_update_status():
        install_dir, _ = resolve_paths()
        src_dir = install_dir.rstrip("/") + "-src"
        info = {"src_available": os.path.isdir(os.path.join(src_dir, ".git"))}
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "rtcview-updater.path"],
                capture_output=True, text=True, timeout=5,
            )
            info["trigger_available"] = r.stdout.strip() == "active"
        except Exception:
            info["trigger_available"] = False
        if info["src_available"]:
            try:
                r = subprocess.run(
                    ["git", "-C", src_dir, "log", "-1", "--format=%h|%ci|%s"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    sha, date, msg = r.stdout.strip().split("|", 2)
                    info.update({"commit": sha, "date": date, "message": msg})
            except Exception as e:
                info["error"] = str(e)
        return jsonify(info)

    @app.post("/api/system/update")
    def api_system_update():
        install_dir, _ = resolve_paths()
        try:
            # systemd's PathExists= only fires on a false->true transition,
            # so a stale trigger file left over from an interrupted prior
            # run (cleanup skipped) must be removed first, not just re-
            # touched, or this update request would silently never fire.
            trigger_path = Path(install_dir, "update.trigger")
            trigger_path.unlink(missing_ok=True)
            trigger_path.touch()
        except Exception as e:
            return jsonify({"error": f"Tetik dosyası yazılamadı: {e}"}), 500
        # The updater .path unit (or the service, moments later) may
        # restart this very process — that's expected.
        return jsonify({"ok": True})

    # ---------- Restart / reboot ----------
    # Same trigger-file + root .path/.service mechanism as self-update
    # above, kept as two SEPARATE trigger files/units (rather than reusing
    # update.trigger) since "just restart the service" and "reboot the
    # whole box" are both far more common and far less involved than a
    # full code update, and conflating them with the updater's path unit
    # would mean every plain restart also re-ran self_update.sh's git
    # fetch/reset. Units generated by install.sh/update.sh.
    def _touch_trigger(name: str):
        install_dir, _ = resolve_paths()
        trigger_path = Path(install_dir, name)
        # Same PathExists=-only-fires-on-false→true-transition reasoning
        # as /api/system/update above -- unlink before touch, not just touch.
        trigger_path.unlink(missing_ok=True)
        trigger_path.touch()

    @app.post("/api/system/restart")
    def api_system_restart():
        try:
            _touch_trigger("restart.trigger")
        except Exception as e:
            return jsonify({"error": f"Tetik dosyası yazılamadı: {e}"}), 500
        return jsonify({"ok": True})

    @app.post("/api/system/reboot")
    def api_system_reboot():
        try:
            _touch_trigger("reboot.trigger")
        except Exception as e:
            return jsonify({"error": f"Tetik dosyası yazılamadı: {e}"}), 500
        return jsonify({"ok": True})

    @app.post("/api/cameras/<camera_id>/record/start")
    def api_rec_manual_start(camera_id):
        body = request.get_json(silent=True) or {}
        try:
            seconds = int(body.get("seconds", MANUAL_DEFAULT_SECONDS) or MANUAL_DEFAULT_SECONDS)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid seconds"}), 400
        ok = recorder.manual_start(camera_id, seconds=seconds)
        if not ok:
            return jsonify({"error": "camera not found or recording disabled"}), 400
        return jsonify({"ok": True, "seconds": seconds})

    @app.post("/api/cameras/<camera_id>/record/stop")
    def api_rec_manual_stop(camera_id):
        ok = recorder.manual_stop(camera_id)
        if not ok: return jsonify({"error": "not recording"}), 404
        return jsonify({"ok": True})

    # ---------- Recording: segments (playback) ----------
    @app.get("/api/recordings")
    def api_recordings():
        cam = request.args.get("cam")
        try:
            t_from = float(request.args.get("from")) if request.args.get("from") else None
            t_to = float(request.args.get("to")) if request.args.get("to") else None
        except ValueError:
            return jsonify({"error": "invalid time range"}), 400
        limit = _int_arg(request.args, "limit", 2000)
        segs = storage.list_segments(cam_id=cam, t_from=t_from, t_to=t_to, limit=limit)
        # Strip absolute path from response — expose only id-referenced URLs.
        for s in segs:
            s["url"] = f"/api/recordings/{s['id']}/stream"
            s["download_url"] = f"/api/recordings/{s['id']}/download"
            del s["path"]
        return jsonify(segs)

    @app.get("/api/recordings/<int:seg_id>")
    def api_recording_meta(seg_id):
        seg = storage.get_segment(seg_id)
        if not seg: return jsonify({"error": "not found"}), 404
        seg["url"] = f"/api/recordings/{seg_id}/stream"
        seg["download_url"] = f"/api/recordings/{seg_id}/download"
        del seg["path"]
        return jsonify(seg)

    @app.get("/api/recordings/<int:seg_id>/stream")
    def api_recording_stream(seg_id):
        seg = storage.get_segment(seg_id)
        if not seg: return jsonify({"error": "not found"}), 404
        return _serve_range(seg["path"], as_attachment=False)

    @app.get("/api/recordings/<int:seg_id>/download")
    def api_recording_download(seg_id):
        seg = storage.get_segment(seg_id)
        if not seg: return jsonify({"error": "not found"}), 404
        return _serve_range(seg["path"], as_attachment=True)

    @app.delete("/api/recordings/<int:seg_id>")
    def api_recording_delete(seg_id):
        force = request.args.get("force") in ("1", "true", "yes")
        ok = storage.delete_segment(seg_id, force=force)
        if not ok: return jsonify({"error": "not found or locked"}), 400
        return jsonify({"ok": True})

    @app.post("/api/recordings/<int:seg_id>/lock")
    def api_recording_lock(seg_id):
        body = request.get_json(silent=True) or {}
        locked = bool(body.get("locked", True))
        ok = storage.set_locked(seg_id, locked)
        if not ok: return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True, "locked": locked})

    # ---------- Snapshots ----------
    @app.post("/api/snapshot/<camera_id>")
    def api_snapshot(camera_id):
        if not _valid_cam_id(camera_id):
            return jsonify({"error": "invalid camera id"}), 400
        cam = _find_camera(camera_id)
        if not cam: return jsonify({"error": "not found"}), 404
        gc = store.get_go2rtc()
        url = f"http://{gc['host']}:{gc['api_port']}/api/frame.jpeg?src={cam.get('stream') or camera_id}"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code != 200 or not r.content:
                return jsonify({"error": f"go2rtc returned {r.status_code}"}), 502
        except Exception as e:
            return jsonify({"error": f"go2rtc unavailable: {e}"}), 502
        save = request.args.get("save", "1") not in ("0", "false", "no")
        payload = {"ok": True, "bytes": len(r.content)}
        if save:
            # Compute date parts ONCE so a saved snapshot near midnight
            # doesn't end up with day-dir=today and filename=tomorrow.
            now_dt = datetime.now()
            root = storage.snapshots_root()
            day_dir = root / camera_id / now_dt.strftime("%Y/%m/%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{camera_id}_{now_dt:%Y%m%d_%H%M%S}.jpg"
            fpath = day_dir / fname
            fpath.write_bytes(r.content)
            sid = storage.register_snapshot(camera_id, str(fpath), now_dt.timestamp())
            payload.update({"id": sid, "url": f"/api/snapshots/{sid}"})
        if request.args.get("return") == "image":
            return Response(r.content, mimetype="image/jpeg")
        return jsonify(payload)

    @app.get("/api/snapshots")
    def api_snapshots_list():
        cam = request.args.get("cam")
        rows = storage.list_snapshots(cam_id=cam, limit=_int_arg(request.args, "limit", 200))
        for s in rows:
            s["url"] = f"/api/snapshots/{s['id']}"
            del s["path"]
        return jsonify(rows)

    @app.get("/api/snapshots/<int:sid>")
    def api_snapshot_get(sid):
        # Minimal fetch: reuse storage._db (small object, safe)
        with storage._lock:
            r = storage._db.execute(
                "SELECT path FROM snapshots WHERE id = ?", (sid,)
            ).fetchone()
        if not r: return jsonify({"error": "not found"}), 404
        return _serve_range(r[0], as_attachment=False)

    # ---------- go2rtc proxy (WHEP + fMP4 stream + API) ----------
    # Live streaming endpoints stay open indefinitely; JSON API endpoints
    # should time out fast so a stalled go2rtc doesn't hang worker threads.
    _STREAMING_ENDPOINTS = ("stream.mp4", "stream.m3u8", "stream.mjpeg", "ws")

    @app.route("/go2rtc/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def go2rtc_proxy(subpath):
        gc = store.get_go2rtc()
        upstream = f"http://{gc['host']}:{gc['api_port']}/{subpath}"
        is_stream = any(subpath.endswith(e) or ("/" + e) in subpath for e in _STREAMING_ENDPOINTS)
        timeout = (5, None) if is_stream else (5, 10)
        try:
            r = requests.request(
                request.method,
                upstream,
                params=request.args,
                data=request.get_data(),
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
                timeout=timeout,
                stream=True,
            )
        except Exception as e:
            return jsonify({"error": f"go2rtc unavailable: {e}"}), 502
        excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        headers = [(k, v) for k, v in r.headers.items() if k.lower() not in excluded]
        # Bir MSE oynatması iptal edildiğinde iter_content generator'ı GC'ye
        # bırakılırsa upstream soketi de sarkıyor — istemci bağlantı kestikçe
        # RAM/soket birikiyor. try/finally + close() ile her koşulda kapat.
        def _pump():
            try:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            finally:
                try: r.close()
                except Exception: pass
        return Response(stream_with_context(_pump()), status=r.status_code, headers=headers)

    return app


# ---------- helpers ----------
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _serve_range(path: str, as_attachment: bool = False):
    """Serve a file with HTTP Range support (required for HTML5 <video> seek)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return jsonify({"error": "file missing"}), 404
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "application/octet-stream"
    fname = os.path.basename(path)

    range_hdr = request.headers.get("Range", "")
    start, end = 0, size - 1
    status = 200
    if range_hdr:
        m = _RANGE_RE.match(range_hdr)
        if m:
            s, e = m.group(1), m.group(2)
            if s == "" and e != "":
                length = int(e)
                start = max(0, size - length); end = size - 1
            else:
                start = int(s or 0)
                end = int(e) if e else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                resp = Response(status=416)
                resp.headers["Content-Range"] = f"bytes */{size}"
                return resp
            status = 206

    length = end - start + 1
    CHUNK = 64 * 1024

    # HEAD wants headers only — don't open the file, don't stream.
    if request.method == "HEAD":
        resp = Response(status=status)
    else:
        def gen():
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        data = f.read(min(CHUNK, remaining))
                        if not data: break
                        remaining -= len(data)
                        yield data
            except OSError as e:
                log.warning("stream aborted for %s: %s", path, e)
                return
        resp = Response(stream_with_context(gen()), status=status, mimetype=mime, direct_passthrough=True)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    resp.headers["Content-Type"] = mime
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    disp = "attachment" if as_attachment else "inline"
    resp.headers["Content-Disposition"] = f'{disp}; filename="{fname}"'
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _which(cmd: str) -> bool:
    import shutil as _sh
    if not cmd: return False
    if os.path.isfile(cmd): return True
    return bool(_sh.which(cmd))


# ---------- System stats (Linux /proc; no extra deps) ----------
# Per-process CPU% needs two samples ~a few 100 ms apart. Cache the previous
# jiffies snapshot so successive calls compute a real percentage without
# blocking sleep() on the request thread.
_CPU_SAMPLES: dict[int, tuple[float, int, int]] = {}


def _read_proc_stat(pid: int):
    """Return (cpu_jiffies, rss_bytes, threads, name, ppid) or None."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            raw = f.read()
    except OSError:
        return None
    # Field 2 is comm in parens (may contain spaces); split around last ')'
    lp = raw.find("(")
    rp = raw.rfind(")")
    if lp < 0 or rp < 0: return None
    name = raw[lp+1:rp]
    rest = raw[rp+2:].split()
    # Adjusted indexes: field 4 (ppid) = rest[1], utime=rest[11], stime=rest[12],
    # num_threads=rest[17], rss (pages)=rest[21]
    try:
        ppid = int(rest[1])
        utime = int(rest[11]); stime = int(rest[12])
        threads = int(rest[17])
        rss_pages = int(rest[21])
    except (IndexError, ValueError):
        return None
    return utime + stime, rss_pages * os.sysconf("SC_PAGE_SIZE"), threads, name, ppid


def _cpu_percent(pid: int) -> float:
    stat = _read_proc_stat(pid)
    if not stat: return 0.0
    cpu, _, _, _, _ = stat
    now = time.time()
    prev = _CPU_SAMPLES.get(pid)
    _CPU_SAMPLES[pid] = (now, cpu, os.sysconf("SC_CLK_TCK"))
    if not prev or now - prev[0] < 0.05:
        return 0.0
    tick = prev[2]
    dt = now - prev[0]
    d_cpu = (cpu - prev[1]) / tick
    return round(max(0.0, min(100.0 * (os.cpu_count() or 1), d_cpu / dt * 100.0)), 1)


def _read_meminfo():
    try:
        d = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                d[k.strip()] = int(rest.strip().split()[0]) * 1024  # kB → B
        return d
    except OSError:
        return {}


def _read_loadavg():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return {"1m": float(parts[0]), "5m": float(parts[1]), "15m": float(parts[2])}
    except (OSError, ValueError, IndexError):
        return {"1m": 0, "5m": 0, "15m": 0}


def _read_cpu_temp(path: str):
    # Linux sysfs thermal-zone files report millidegrees C — this is the
    # universal format for /sys/class/thermal/thermal_zone*/temp
    # regardless of board. Returns None (not an error) for an empty path,
    # a missing zone, or any unreadable/non-numeric content, since the
    # right zone number is board-specific and the configured path may not
    # exist on this particular device.
    if not path:
        return None
    try:
        with open(path) as f:
            raw = f.read().strip()
        return round(int(raw) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def _read_system_stats(recorder, store):
    my_pid = os.getpid()
    my_stat = _read_proc_stat(my_pid)
    my_cpu = _cpu_percent(my_pid)
    mem = _read_meminfo()
    load = _read_loadavg()

    # _CPU_SAMPLES is keyed by PID; ffmpeg restarts leak entries forever
    # if we never prune. Drop entries whose /proc/<pid> is gone.
    if _CPU_SAMPLES:
        for _pid in [p for p in _CPU_SAMPLES if not os.path.exists(f"/proc/{p}")]:
            _CPU_SAMPLES.pop(_pid, None)

    process = {
        "pid": my_pid,
        "cpu_percent": my_cpu,
        "rss": my_stat[1] if my_stat else 0,
        "threads": my_stat[2] if my_stat else 0,
        "name": my_stat[3] if my_stat else "rtcview",
    }

    # Walk /proc to find ffmpeg children of this rtcview process
    ffmpeg = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit(): continue
            pid = int(entry)
            if pid == my_pid: continue
            s = _read_proc_stat(pid)
            if not s: continue
            _, rss, threads, name, ppid = s
            if ppid != my_pid: continue
            if "ffmpeg" not in name.lower(): continue
            ffmpeg.append({
                "pid": pid,
                "name": name,
                "cpu_percent": _cpu_percent(pid),
                "rss": rss,
                "threads": threads,
            })
    except OSError:
        pass

    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    mem_used = mem_total - mem_avail if mem_total else 0
    temp_c = _read_cpu_temp(store.get_app().get("temp_sensor_path", ""))

    return {
        "process": process,
        "ffmpeg": sorted(ffmpeg, key=lambda x: -x["cpu_percent"]),
        "system": {
            "cpu_count": os.cpu_count() or 1,
            "load": load,
            "mem_total": mem_total,
            "mem_used": mem_used,
            "mem_free": mem.get("MemFree", 0),
            "mem_available": mem_avail,
            "swap_total": mem.get("SwapTotal", 0),
            "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
            "temp_c": temp_c,
        },
        "recorders": len(recorder._recs) if hasattr(recorder, "_recs") else 0,
    }


def port_available(port: int, host: str = "0.0.0.0") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def main():
    parser = argparse.ArgumentParser("rtcview")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", type=str, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--dev", action="store_true")
    args = parser.parse_args()

    _, config_dir = resolve_paths()
    config_path = args.config or os.path.join(config_dir, "config.json")
    Path(os.path.dirname(config_path)).mkdir(parents=True, exist_ok=True)

    app = create_app(config_path)
    store = app.config["STORE"]
    ac = store.get_app()
    host = args.host or ac.get("host", "0.0.0.0")
    port = args.port or int(ac.get("port", 5000))

    if not port_available(port, host):
        log.error("Port %s is already in use on %s.", port, host)
        sys.exit(2)

    log.info("RtcView başladı — http://%s:%s adresinde dinleniyor", host, port)
    if args.dev:
        app.run(host=host, port=port, debug=True, use_reloader=False)
    else:
        from waitress import serve
        # 10 threads is plenty for a LAN camera dashboard on rk3399; 16
        # was context-switch heavy for a 6-core SoC.
        serve(app, host=host, port=port, threads=10)


if __name__ == "__main__":
    main()
