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
from app.detection import DetectionManager, ONVIF_AVAILABLE as ONVIF_EVENTS_AVAILABLE
from app.go2rtc_client import Go2RtcClient
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
    # everywhere one can originate from user input, not just here.
    _CAM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

    store = ConfigStore(config_path)
    go2rtc = Go2RtcClient(store)
    storage = Storage(store)
    recorder = RecordingManager(store, storage)
    detector = DetectionManager(store, storage)

    app.config["STORE"] = store
    app.config["GO2RTC"] = go2rtc
    app.config["STORAGE"] = storage
    app.config["RECORDER"] = recorder
    app.config["DETECTOR"] = detector

    storage.start()
    recorder.start()
    detector.start()

    def _shutdown():
        try: detector.stop()
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
            "onvif_events_available": ONVIF_EVENTS_AVAILABLE,
            "recording_enabled": store.get_recording().get("enabled", True),
            "version": "1.1.0",
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
                   "show_status_badges", "auto_reconnect", "reconnect_delay_ms"}
        clean = {k: v for k, v in updates.items() if k in allowed}
        if "grid_columns" in clean:
            try:
                gc = int(clean["grid_columns"])
                if gc < 1 or gc > 8:
                    return jsonify({"error": "grid_columns must be 1-8"}), 400
                clean["grid_columns"] = gc
            except Exception:
                return jsonify({"error": "invalid grid_columns"}), 400
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
        try:
            motion_timeout = max(5, min(300, int(body.get("motion_timeout_seconds", 15) or 15)))
        except (TypeError, ValueError):
            motion_timeout = 15
        # cam_id becomes a directory name under every storage root
        # (_cam_dir in recorder.py) and a dict key workers are indexed by —
        # an unvalidated id from the client is a path-traversal vector
        # ("../../etc") and a duplicate id would silently desync in-memory
        # recorder/detector state from the config list.
        cam_id = body.get("id") or "cam_" + uuid.uuid4().hex[:8]
        if not _CAM_ID_RE.match(cam_id):
            return jsonify({"error": "invalid camera id"}), 400
        if any(c["id"] == cam_id for c in store.get_cameras()):
            return jsonify({"error": "camera id already exists"}), 400
        cam = {
            "id": cam_id,
            "name": name,
            "stream": stream,
            "ptz_enabled": bool(body.get("ptz_enabled", False)),
            "onvif_host": body.get("onvif_host", ""),
            "onvif_port": int(body.get("onvif_port", 80) or 80),
            "onvif_user": body.get("onvif_user", ""),
            "onvif_pass": body.get("onvif_pass", ""),
            "record_mode": body.get("record_mode", "off"),
            "record_schedule": body.get("record_schedule", []),
            "record_audio": bool(body.get("record_audio", False)),
            "retention_days_override": int(body.get("retention_days_override", 0) or 0),
            "motion_detection_enabled": bool(body.get("motion_detection_enabled", False)),
            "person_detection_enabled": bool(body.get("person_detection_enabled", False)),
            "motion_timeout_seconds": motion_timeout,
            "group_ids": [str(g) for g in (body.get("group_ids") or []) if str(g).strip()],
        }
        store.add_camera(cam)
        recorder.reload_camera(cam["id"])
        detector.reload_camera(cam["id"])
        return jsonify(cam), 201

    @app.put("/api/cameras/<camera_id>")
    def api_update_camera(camera_id):
        body = request.get_json(force=True) or {}
        # Legacy field cleanup: older configs stored stream_mode per camera;
        # transport is now a device-scoped localStorage preference, so drop
        # any incoming stream_mode value.
        body.pop("stream_mode", None)
        if "motion_timeout_seconds" in body:
            try:
                body["motion_timeout_seconds"] = max(5, min(300, int(body["motion_timeout_seconds"] or 15)))
            except (TypeError, ValueError):
                return jsonify({"error": "invalid motion_timeout_seconds"}), 400
        if "group_ids" in body:
            body["group_ids"] = [str(g) for g in (body.get("group_ids") or []) if str(g).strip()]
        ok = store.update_camera(camera_id, body)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ptz_controller.invalidate(camera_id)
        recorder.reload_camera(camera_id)
        detector.reload_camera(camera_id)
        return jsonify({"ok": True})

    @app.delete("/api/cameras/<camera_id>")
    def api_delete_camera(camera_id):
        ok = store.remove_camera(camera_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ptz_controller.invalidate(camera_id)
        recorder.reload_camera(camera_id)
        detector.reload_camera(camera_id)
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
    @app.get("/api/groups")
    def api_groups_list():
        return jsonify(store.get_groups())

    @app.post("/api/groups")
    def api_groups_add():
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        group = {"id": "grp_" + uuid.uuid4().hex[:8], "name": name}
        store.add_group(group)
        return jsonify(group), 201

    @app.put("/api/groups/<group_id>")
    def api_groups_update(group_id):
        body = request.get_json(force=True) or {}
        updates = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                return jsonify({"error": "name required"}), 400
            updates["name"] = name
        if "notify_enabled" in body:
            updates["notify_enabled"] = bool(body["notify_enabled"])
        if "notify_schedule" in body:
            # Scheduled on/off ACTIONS (not windows) — see
            # _notify_rules_active in detection.py. Normalised here so a
            # malformed row can never reach the evaluator or the config
            # file: bad times are rejected outright rather than silently
            # ignored later, since a dropped rule changes when the user
            # actually gets notified.
            rules = []
            for r in (body.get("notify_schedule") or []):
                if not isinstance(r, dict):
                    return jsonify({"error": "invalid notify_schedule row"}), 400
                try:
                    hh, mm = (int(x) for x in str(r.get("time", "")).split(":", 1))
                except (TypeError, ValueError):
                    return jsonify({"error": "invalid notify_schedule time"}), 400
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    return jsonify({"error": "invalid notify_schedule time"}), 400
                days = []
                for d in (r.get("days") or []):
                    try:
                        d = int(d)
                    except (TypeError, ValueError):
                        return jsonify({"error": "invalid notify_schedule day"}), 400
                    if not 0 <= d <= 6:
                        return jsonify({"error": "invalid notify_schedule day"}), 400
                    days.append(d)
                rules.append({
                    "days": sorted(set(days)),
                    "time": f"{hh:02d}:{mm:02d}",
                    "action": "off" if str(r.get("action", "on")).lower() == "off" else "on",
                })
            updates["notify_schedule"] = rules
            # notify_rule_applied_at is a monotone high-water mark on the
            # timestamp of the last-applied occurrence (see
            # apply_notify_rules in detection.py) — it only ever compares
            # against occurrences of the CURRENT schedule. Editing the
            # schedule (e.g. moving a rule earlier in the day, or adding
            # one) can make "most recent occurrence <= now" compute to a
            # timestamp at or before that stale mark, and it would then be
            # silently treated as "already applied" for up to a week.
            # Resetting the mark makes the next tick recompute from
            # scratch — the same self-correcting path already used for a
            # boundary missed while the service was stopped.
            updates["notify_rule_applied_at"] = 0
        if not updates:
            return jsonify({"error": "no valid fields"}), 400
        ok = store.update_group(group_id, updates)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify(next((g for g in store.get_groups() if g["id"] == group_id), {"ok": True}))

    @app.delete("/api/groups/<group_id>")
    def api_groups_delete(group_id):
        ok = store.remove_group(group_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    # ---------- PTZ ----------
    def _find_camera(camera_id):
        for c in store.get_cameras():
            if c["id"] == camera_id:
                return c
        return None

    # ---------- Motion / person detection (ONVIF) ----------
    @app.get("/api/detection/status")
    def api_detection_status():
        # Optional ?cam=<id> narrows the response to one camera. Each entry
        # carries a long debug log, so the UI (which shows one camera at a
        # time) asks for just that one; omitting the arg keeps the original
        # all-cameras behaviour.
        return jsonify(detector.status(cam_id=request.args.get("cam") or None))

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

    @app.post("/api/cameras/<camera_id>/detection/test")
    def api_detection_test(camera_id):
        cam = _find_camera(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        return jsonify(detector.test_connection(cam))

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
        pan = float(body.get("pan", 0))
        tilt = float(body.get("tilt", 0))
        zoom = float(body.get("zoom", 0))
        timeout = float(body.get("timeout", 0.6))
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
        allowed = {"enabled", "storage_path", "storage_paths", "segment_seconds",
                   "retention_days", "max_gb", "purge_interval_seconds", "ffmpeg_path"}
        clean = {k: v for k, v in body.items() if k in allowed}
        # Accept both new list and legacy scalar; normalize to a list.
        new_paths = clean.pop("storage_paths", None)
        new_path  = clean.pop("storage_path", None)
        if new_paths is None and new_path is not None:
            new_paths = [new_path]
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
        if "purge_interval_seconds" in clean:
            # The purge loop does self._stop.wait(purge_interval_seconds)
            # between full-table retention/quota scans; 0 (or negative)
            # makes it a busy loop pegging a core forever.
            clean["purge_interval_seconds"] = max(5, clean["purge_interval_seconds"])
        if "enabled" in clean: clean["enabled"] = bool(clean["enabled"])
        # Storage-list change is atomic + recorder-safe: stop cleanly, swap,
        # start again. Current segment is finalised via the graceful stop.
        # storage_paths can be either strings or {path, max_gb} objects.
        current_entries = storage.roots_with_quota()
        current_key = [(e["path"], e["max_gb"]) for e in current_entries]
        moving_roots = False
        wanted_normalized = None
        if new_paths is not None:
            wanted_normalized = []
            for np in new_paths:
                if isinstance(np, dict):
                    p = str(np.get("path") or "").strip()
                    try: q = max(0, int(np.get("max_gb", 0) or 0))
                    except (TypeError, ValueError): q = 0
                else:
                    p = str(np or "").strip(); q = 0
                if not p: continue
                wanted_normalized.append({
                    "path": str(Path(p).expanduser().resolve()),
                    "max_gb": q,
                })
            wanted_key = [(e["path"], e["max_gb"]) for e in wanted_normalized]
            moving_roots = wanted_key != current_key
        if moving_roots:
            recorder.stop()
        if clean: store.update_recording(clean)
        if wanted_normalized is not None:
            ok, msg = storage.set_roots(wanted_normalized)
            if not ok:
                if moving_roots: recorder.start()
                return jsonify({"error": msg}), 400
        if moving_roots:
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
        return jsonify(_read_system_stats(recorder))

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

    @app.post("/api/cameras/<camera_id>/record/start")
    def api_rec_manual_start(camera_id):
        body = request.get_json(silent=True) or {}
        seconds = int(body.get("seconds", MANUAL_DEFAULT_SECONDS) or MANUAL_DEFAULT_SECONDS)
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
        if not _CAM_ID_RE.match(camera_id or ""):
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


def _read_system_stats(recorder):
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

    log.info("RtcView starting on http://%s:%s", host, port)
    if args.dev:
        app.run(host=host, port=port, debug=True, use_reloader=False)
    else:
        from waitress import serve
        # 10 threads is plenty for a LAN camera dashboard on rk3399; 16
        # was context-switch heavy for a 6-core SoC.
        serve(app, host=host, port=port, threads=10)


if __name__ == "__main__":
    main()
