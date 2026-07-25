import argparse
import atexit
import logging
import mimetypes
import os
import re
import socket
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context
from flask_cors import CORS

from app.config import ConfigStore
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


def create_app(config_path: str) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    CORS(app)

    store = ConfigStore(config_path)
    go2rtc = Go2RtcClient(store)
    storage = Storage(store)
    recorder = RecordingManager(store, storage)

    app.config["STORE"] = store
    app.config["GO2RTC"] = go2rtc
    app.config["STORAGE"] = storage
    app.config["RECORDER"] = recorder

    storage.start()
    recorder.start()

    def _shutdown():
        try: recorder.stop()
        except Exception: pass
        try: storage.stop()
        except Exception: pass
    atexit.register(_shutdown)

    # ---------- Pages ----------
    def _asset_version():
        try:
            base = Path(app.static_folder)
            latest = max(
                (p.stat().st_mtime for p in base.rglob("*") if p.is_file()),
                default=0,
            )
            return str(int(latest))
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
        store.data["go2rtc"].update(clean)
        store.save()
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
        cam = {
            "id": body.get("id") or "cam_" + uuid.uuid4().hex[:8],
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
        }
        store.add_camera(cam)
        recorder.reload_camera(cam["id"])
        return jsonify(cam), 201

    @app.put("/api/cameras/<camera_id>")
    def api_update_camera(camera_id):
        body = request.get_json(force=True) or {}
        ok = store.update_camera(camera_id, body)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ptz_controller.invalidate(camera_id)
        recorder.reload_camera(camera_id)
        return jsonify({"ok": True})

    @app.delete("/api/cameras/<camera_id>")
    def api_delete_camera(camera_id):
        ok = store.remove_camera(camera_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        ptz_controller.invalidate(camera_id)
        recorder.reload_camera(camera_id)
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

    # ---------- Recording: settings, status ----------
    @app.get("/api/recording/settings")
    def api_rec_settings_get():
        return jsonify(store.get_recording())

    @app.post("/api/recording/settings")
    def api_rec_settings_set():
        body = request.get_json(force=True) or {}
        allowed = {"enabled", "storage_path", "segment_seconds",
                   "retention_days", "max_gb", "purge_interval_seconds", "ffmpeg_path"}
        clean = {k: v for k, v in body.items() if k in allowed}
        # Path change is validated separately (writability check).
        new_path = clean.pop("storage_path", None)
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
        if "enabled" in clean: clean["enabled"] = bool(clean["enabled"])
        if clean: store.update_recording(clean)
        if new_path is not None:
            ok, msg = storage.set_root(str(new_path))
            if not ok:
                return jsonify({"error": msg}), 400
        recorder.reload_all()
        return jsonify(store.get_recording())

    @app.get("/api/recording/status")
    def api_rec_status():
        return jsonify({
            "settings": store.get_recording(),
            "cameras": recorder.status(),
            "storage": storage.stats(),
            "ffmpeg_available": bool(_which(store.get_recording().get("ffmpeg_path") or "ffmpeg")),
        })

    @app.post("/api/recording/rescan")
    def api_rec_rescan():
        return jsonify(storage.rescan())

    @app.post("/api/recording/purge")
    def api_rec_purge():
        return jsonify(storage.purge_once())

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
        limit = int(request.args.get("limit", 2000))
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
            root = storage.snapshots_root()
            day_dir = root / camera_id / datetime.now().strftime("%Y/%m/%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{camera_id}_{datetime.now():%Y%m%d_%H%M%S}.jpg"
            fpath = day_dir / fname
            fpath.write_bytes(r.content)
            sid = storage.register_snapshot(camera_id, str(fpath), time.time())
            payload.update({"id": sid, "url": f"/api/snapshots/{sid}"})
        # Also return the raw JPEG so the client can preview/download without a round-trip.
        if request.args.get("return") == "image":
            return Response(r.content, mimetype="image/jpeg")
        return jsonify(payload)

    @app.get("/api/snapshots")
    def api_snapshots_list():
        cam = request.args.get("cam")
        rows = storage.list_snapshots(cam_id=cam, limit=int(request.args.get("limit", 200)))
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

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                data = f.read(min(CHUNK, remaining))
                if not data: break
                remaining -= len(data)
                yield data

    resp = Response(stream_with_context(gen()), status=status, mimetype=mime, direct_passthrough=True)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
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
