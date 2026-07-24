import argparse
import logging
import os
import socket
import sys
import uuid
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from flask_cors import CORS

from app.config import ConfigStore
from app.go2rtc_client import Go2RtcClient
from app.ptz import ptz_controller, ONVIF_AVAILABLE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rtcview")


def resolve_paths():
    install_dir = os.environ.get("RTCVIEW_HOME", "/opt/rtcview")
    config_dir = os.environ.get("RTCVIEW_CONFIG", os.path.join(install_dir, "config"))
    return install_dir, config_dir


def create_app(config_path: str) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    CORS(app)

    store = ConfigStore(config_path)
    go2rtc = Go2RtcClient(store)

    app.config["STORE"] = store
    app.config["GO2RTC"] = go2rtc

    # ---------- Pages ----------
    @app.route("/")
    def index():
        return render_template("index.html")

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
            "version": "1.0.0",
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
        allowed = {"host", "api_port"}
        clean = {k: v for k, v in body.items() if k in allowed}
        if "api_port" in clean:
            try: clean["api_port"] = int(clean["api_port"])
            except Exception: return jsonify({"error": "invalid api_port"}), 400
        store.data["go2rtc"].update(clean)
        store.save()
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
        cam = {
            "id": body.get("id") or "cam_" + uuid.uuid4().hex[:8],
            "name": name,
            "stream": stream,
            "ptz_enabled": bool(body.get("ptz_enabled", False)),
            "onvif_host": body.get("onvif_host", ""),
            "onvif_port": int(body.get("onvif_port", 80) or 80),
            "onvif_user": body.get("onvif_user", ""),
            "onvif_pass": body.get("onvif_pass", ""),
        }
        store.add_camera(cam)
        return jsonify(cam), 201

    @app.put("/api/cameras/<camera_id>")
    def api_update_camera(camera_id):
        body = request.get_json(force=True) or {}
        ok = store.update_camera(camera_id, body)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ptz_controller.invalidate(camera_id)
        return jsonify({"ok": True})

    @app.delete("/api/cameras/<camera_id>")
    def api_delete_camera(camera_id):
        ok = store.remove_camera(camera_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ptz_controller.invalidate(camera_id)
        return jsonify({"ok": True})

    @app.post("/api/cameras/reorder")
    def api_reorder():
        body = request.get_json(force=True) or {}
        order = body.get("order", [])
        if not isinstance(order, list):
            return jsonify({"error": "order must be a list"}), 400
        store.reorder_cameras(order)
        return jsonify({"ok": True})

    # ---------- PTZ ----------
    def _find_camera(camera_id):
        for c in store.get_cameras():
            if c["id"] == camera_id:
                return c
        return None

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

    # ---------- go2rtc proxy (WHEP + API) ----------
    @app.route("/go2rtc/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def go2rtc_proxy(subpath):
        gc = store.get_go2rtc()
        upstream = f"http://{gc['host']}:{gc['api_port']}/{subpath}"
        try:
            r = requests.request(
                request.method,
                upstream,
                params=request.args,
                data=request.get_data(),
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
                timeout=10,
                stream=True,
            )
        except Exception as e:
            return jsonify({"error": f"go2rtc unavailable: {e}"}), 502
        excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        headers = [(k, v) for k, v in r.headers.items() if k.lower() not in excluded]
        return Response(r.iter_content(chunk_size=8192), status=r.status_code, headers=headers)

    return app


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
        serve(app, host=host, port=port, threads=16)


if __name__ == "__main__":
    main()
