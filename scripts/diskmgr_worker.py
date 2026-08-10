#!/usr/bin/env python3
"""RtcView disk-manager worker — root-privileged half.

Invoked by rtcview-diskmgr.service (root, oneshot), itself triggered by
rtcview-diskmgr.path watching ${RTCVIEW_HOME}/diskmgr/jobs/ for new files
(DirectoryNotEmpty=). Mirrors self_update.sh's trigger-file pattern
(scripts/self_update.sh, rtcview-updater.path/.service in install.sh) but
bidirectional: reads a small JSON job written by the unprivileged Flask
app (app/diskmgr.py's submit_job()) and writes a JSON result back.

Deliberately never imports anything from the Flask app itself (app/main.py,
app/config.py) — this process runs as root and touches raw block devices;
keeping it import-isolated from the web app keeps "code that can destroy a
disk" auditable as its own small file. It DOES import the pure, subprocess-
free helpers from app/diskmgr.py (whole_disk_of/excluded_devices) so the
OS-disk exclusion logic is defined exactly once and re-derived independently
here rather than trusting anything the job file claims.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# app/diskmgr.py lives at <install_dir>/app/diskmgr.py; this script lives
# at <install_dir>/scripts/diskmgr_worker.py — Python only puts the
# script's OWN directory on sys.path by default, not its parent, so
# `from app.diskmgr import ...` needs an explicit path add regardless of
# the invoking cwd (systemd units don't reliably set one).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.diskmgr import whole_disk_of, excluded_devices, _run_lsblk  # noqa: E402

INSTALL_DIR = os.environ.get("RTCVIEW_HOME", "/opt/rtcview")
JOBS_DIR = Path(INSTALL_DIR) / "diskmgr" / "jobs"
RESULTS_DIR = Path(INSTALL_DIR) / "diskmgr" / "results"
SERVICE_USER = os.environ.get("RTCVIEW_SERVICE_USER", "rtcview")


def log(msg: str):
    print(f"[diskmgr_worker] {msg}", flush=True)


def _run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _write_result(job_id: str, ok: bool, error: str = None, **extra):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result = {"ok": ok, "error": error, **extra}
    path = RESULTS_DIR / f"{job_id}.json"
    tmp = RESULTS_DIR / f"{job_id}.json.part"
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(result, f)
    os.rename(tmp, path)
    os.chmod(path, 0o644)  # don't rely on this root process's default umask
    if not ok:
        log(f"job {job_id} FAILED: {error}")
    else:
        log(f"job {job_id} ok: {extra}")


def _mounted_here(by_path: dict, device: str) -> bool:
    node = by_path.get(device)
    if node is None:
        return False
    if node.get("mountpoint"):
        return True
    for child in node.get("children") or []:
        if child.get("mountpoint"):
            return True
    return False


def _is_swap(device: str) -> bool:
    try:
        with open("/proc/swaps") as f:
            lines = f.readlines()[1:]
        swap_paths = set()
        for line in lines:
            parts = line.split()
            if parts:
                swap_paths.add(parts[0])
                swap_paths.add(os.path.realpath(parts[0]))
        return device in swap_paths or os.path.realpath(device) in swap_paths
    except OSError:
        return False


def _revalidate_device(device: str) -> str:
    """Returns an error string if `device` must NOT be touched, else "".
    Independently re-derives everything -- never trusts the job file."""
    try:
        by_name, by_path = _run_lsblk()
    except Exception as e:
        return f"lsblk çalıştırılamadı: {e}"
    excluded, ok = excluded_devices(by_path, by_name)
    if not ok:
        return "sistem diski belirlenemedi — güvenlik için hiçbir işlem yapılmadı"
    if device not in by_path:
        return f"cihaz bulunamadı: {device}"
    whole = whole_disk_of(device, by_path, by_name)
    if whole is not None and whole in excluded:
        return "bu cihaz sistem diskinin bir parçası — dokunulamaz"
    if _is_swap(device):
        return "bu cihaz takas (swap) alanı — dokunulamaz"
    return ""


def _handle_format(job: dict) -> tuple:
    device = job.get("device")
    fstype = job.get("fstype")
    label = (job.get("label") or "")[:16]  # ext4/f2fs label length limits
    if not device or fstype not in ("ext4", "f2fs"):
        return False, "geçersiz format isteği", {}

    err = _revalidate_device(device)
    if err:
        return False, err, {}

    by_name, by_path = _run_lsblk()
    if _mounted_here(by_path, device):
        return False, "cihaz bağlı — önce ayırın", {}

    r = _run(["wipefs", "-a", device])
    if r.returncode != 0:
        return False, f"wipefs başarısız: {r.stderr.strip()}", {}

    if fstype == "ext4":
        cmd = ["mkfs.ext4", "-F"]
        if label:
            cmd += ["-L", label]
        cmd.append(device)
    else:
        cmd = ["mkfs.f2fs", "-f"]
        if label:
            cmd += ["-l", label]
        cmd.append(device)
    r = _run(cmd, timeout=300)
    if r.returncode != 0:
        return False, f"{cmd[0]} başarısız: {r.stderr.strip()}", {}

    _run(["udevadm", "settle", "--timeout", "10"])
    r = _run(["blkid", "-o", "value", "-s", "UUID", device])
    disk_uuid = (r.stdout or "").strip() or None
    return True, None, {"device": device, "fstype": fstype, "uuid": disk_uuid}


def _handle_mount(job: dict) -> tuple:
    device = job.get("device")
    mountpoint = job.get("mountpoint")
    # Purely cosmetic passthrough for the Depolama page's disk rows (set
    # by whoever formatted the disk, carried by the Flask route from the
    # request body) -- never used for any safety decision here.
    label = job.get("label") or ""
    if not device or not mountpoint:
        return False, "geçersiz bağlama isteği", {}
    if not mountpoint.startswith("/"):
        return False, "mountpoint mutlak yol olmalı", {}

    err = _revalidate_device(device)
    if err:
        return False, err, {}

    by_name, by_path = _run_lsblk()
    if _mounted_here(by_path, device):
        return False, "cihaz zaten bağlı", {}

    r = _run(["blkid", "-o", "value", "-s", "UUID", device])
    disk_uuid = (r.stdout or "").strip()
    if not disk_uuid:
        return False, "dosya sistemi bulunamadı — önce formatlayın", {}
    r = _run(["blkid", "-o", "value", "-s", "TYPE", device])
    fstype = (r.stdout or "").strip() or "auto"

    try:
        os.makedirs(mountpoint, exist_ok=True)
    except OSError as e:
        return False, f"mountpoint oluşturulamadı: {e}", {}

    by_uuid_path = f"/dev/disk/by-uuid/{disk_uuid}"
    r = _run(["mount", "-t", fstype, by_uuid_path, mountpoint])
    if r.returncode != 0:
        return False, f"mount başarısız: {r.stderr.strip()}", {}

    r = _run(["chown", f"{SERVICE_USER}:{SERVICE_USER}", mountpoint])
    if r.returncode != 0:
        # Mount already succeeded -- surface the chown failure but don't
        # unmount over it; the admin can fix ownership by hand and the
        # mount is still usable read-side.
        _run(["udevadm", "settle", "--timeout", "10"])
        return True, None, {
            "device": device, "mountpoint": mountpoint, "fstype": fstype,
            "uuid": disk_uuid, "label": label, "warning": f"chown başarısız: {r.stderr.strip()}",
        }

    _run(["udevadm", "settle", "--timeout", "10"])
    return True, None, {"device": device, "mountpoint": mountpoint, "fstype": fstype,
                         "uuid": disk_uuid, "label": label}


def _handle_unmount(job: dict) -> tuple:
    mountpoint = job.get("mountpoint")
    if not mountpoint or not mountpoint.startswith("/"):
        return False, "geçersiz ayırma isteği", {}

    # Re-derive what's actually mounted there and refuse if it resolves
    # onto the excluded (system/boot) disk -- same safety primitive used
    # for format/mount, applied uniformly rather than a separate
    # path-based denylist.
    r = _run(["findmnt", "-no", "SOURCE", mountpoint])
    src = (r.stdout or "").strip()
    if r.returncode != 0 or not src:
        return False, "bu yolda bağlı bir dosya sistemi yok", {}
    real = os.path.realpath(src)
    try:
        by_name, by_path = _run_lsblk()
    except Exception as e:
        return False, f"lsblk çalıştırılamadı: {e}", {}
    excluded, ok = excluded_devices(by_path, by_name)
    if not ok:
        return False, "sistem diski belirlenemedi — güvenlik için hiçbir işlem yapılmadı", {}
    whole = whole_disk_of(real, by_path, by_name)
    if whole is not None and whole in excluded:
        return False, "bu bağlama noktası sistem diskinin bir parçası — dokunulamaz", {}

    r = _run(["umount", mountpoint])
    if r.returncode != 0:
        return False, f"umount başarısız: {r.stderr.strip()}", {}
    return True, None, {"mountpoint": mountpoint}


HANDLERS = {"format": _handle_format, "mount": _handle_mount, "unmount": _handle_unmount}


def process_job(path: Path):
    job_id = path.stem
    try:
        with path.open("r", encoding="utf-8") as f:
            job = json.load(f)
    except Exception as e:
        _write_result(job_id, False, f"job dosyası okunamadı: {e}")
        path.unlink(missing_ok=True)
        return

    action = job.get("action")
    handler = HANDLERS.get(action)
    if handler is None:
        _write_result(job_id, False, f"bilinmeyen işlem: {action}")
        path.unlink(missing_ok=True)
        return

    try:
        ok, error, extra = handler(job)
    except Exception as e:
        ok, error, extra = False, f"beklenmeyen hata: {e}", {}

    _write_result(job_id, ok, error, action=action, **extra)
    path.unlink(missing_ok=True)


def main():
    if os.geteuid() != 0:
        log("root olarak çalıştırılmalı")
        sys.exit(1)
    if not JOBS_DIR.exists():
        return
    # Glob ALL pending jobs (a .path unit firing can coalesce several
    # rapid changes into one invocation), sorted by filename -- the
    # time.time_ns() prefix submit_job() uses makes this submission order.
    jobs = sorted(p for p in JOBS_DIR.glob("*.json") if not p.name.endswith(".part"))
    for job_path in jobs:
        process_job(job_path)


if __name__ == "__main__":
    main()
