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
    # Cap request bodies at 2 MB so a stray/malicious POST can't OOM the
    # server. All real payloads (config, camera CRUD, recording settings)
    # are far below this.
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
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
        stream_mode = (body.get("stream_mode") or "auto").lower()
        if stream_mode not in ("auto", "webrtc", "mse"):
            stream_mode = "auto"
        cam = {
            "id": body.get("id") or "cam_" + uuid.uuid4().hex[:8],
            "name": name,
            "stream": stream,
            "stream_mode": stream_mode,
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
        if "stream_mode" in body:
            sm = str(body.get("stream_mode") or "auto").lower()
            body["stream_mode"] = sm if sm in ("auto", "webrtc", "mse") else "auto"
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
        new_path = clean.pop("storage_path", None)
        # Validate everything BEFORE mutating anything — an invalid field
        # must not leave the recording config half-updated.
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
        # If the storage root is moving, stop the recorders BEFORE the switch
        # so the currently-writing segment doesn't get orphaned between the
        # old path (still on disk) and the new index (points elsewhere).
        moving_root = new_path is not None and str(Path(new_path).expanduser().resolve()) != \
            str(Path(store.get_recording().get("storage_path", "")).expanduser().resolve())
        if moving_root:
            recorder.stop()
        if clean: store.update_recording(clean)
        if new_path is not None:
            ok, msg = storage.set_root(str(new_path))
            if not ok:
                if moving_root: recorder.start()
                return jsonify({"error": msg}), 400
        if moving_root:
            recorder.start()
        else:
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

    # ---------- System stats + logs ----------
    @app.get("/api/system/stats")
    def api_system_stats():
        return jsonify(_read_system_stats(recorder))

    @app.get("/api/system/logs")
    def api_system_logs():
        try: lines = max(20, min(1000, int(request.args.get("lines", 200))))
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
    _CAM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

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

    # ---------- go2rtc proxy (WHEP + fMP4 stream + API) ----------
    @app.route("/go2rtc/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def go2rtc_proxy(subpath):
        gc = store.get_go2rtc()
        upstream = f"http://{gc['host']}:{gc['api_port']}/{subpath}"
        # Live streams (fMP4, HLS, MJPEG) are long-poll style. Use a short
        # connect timeout and NO read timeout so a live upstream can stay
        # open forever without the proxy tearing it down.
        try:
            r = requests.request(
                request.method,
                upstream,
                params=request.args,
                data=request.get_data(),
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
                timeout=(5, None),
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
    return round(max(0.0, min(100.0 * os.cpu_count() or 1, d_cpu / dt * 100.0)), 1)


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
        serve(app, host=host, port=port, threads=16)


if __name__ == "__main__":
    main()
