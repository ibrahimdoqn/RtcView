"""Physical disk management: unprivileged half.

rtcview.service runs as an unprivileged user with NoNewPrivileges=yes — it
can list block devices (lsblk/findmnt just read /sys + the udev database,
no root needed) but can never format, mount, or chown anything itself.
Destructive/privileged actions are handed off to a root-owned systemd
service via a job-queue directory, mirroring the self-update mechanism
(scripts/self_update.sh, the rtcview-updater.path/.service pair) — the app
writes a small JSON job file, a separate root .path unit notices it and
runs scripts/diskmgr_worker.py, which writes a result file back here.

whole_disk_of()/excluded_devices() are pure functions (no privileged
subprocess calls) deliberately kept import-safe for scripts/diskmgr_worker.py
to reuse verbatim for its own independent re-validation — the root worker
NEVER trusts a job file's claim about which device it's targeting; it
re-derives the OS-disk exclusion itself from scratch every time.
"""
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

# Mountpoints that must never resolve to a formattable/mountable candidate.
# "/" is mandatory -- every Linux box has a root mount, so a failure to
# resolve it is treated as "something is wrong, exclude everything" rather
# than "this box has no boot disk". The others are optional: plenty of
# boards (e.g. no separate /boot) simply won't have them, and that's fine.
_BOOT_MOUNTPOINTS = ("/", "/boot", "/boot/efi", "/boot/firmware")


class DiskJobConflict(Exception):
    """Raised when a job is already pending for the same device/uuid."""


def _install_dir() -> str:
    # Deliberately NOT imported from app.main.resolve_paths() -- app.main
    # imports this module, so importing back would be circular. Same two
    # lines, same env var, kept in sync by inspection (both tiny).
    return os.environ.get("RTCVIEW_HOME", "/opt/rtcview")


def _jobs_dir() -> Path:
    d = Path(_install_dir()) / "diskmgr" / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _results_dir() -> Path:
    d = Path(_install_dir()) / "diskmgr" / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- lsblk parsing ----------

def _flatten(devices, by_name: dict, by_path: dict):
    for d in devices or []:
        name = d.get("name")
        path = d.get("path") or (f"/dev/{name}" if name else None)
        if name:
            by_name[name] = d
        if path:
            by_path[path] = d
        children = d.get("children") or []
        if children:
            _flatten(children, by_name, by_path)


def _run_lsblk():
    r = subprocess.run(
        ["lsblk", "-J", "-b", "-o",
         "NAME,PATH,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,TYPE,RM,RO,PKNAME,MODEL"],
        capture_output=True, text=True, timeout=10,
    )
    data = json.loads(r.stdout or "{}")
    by_name, by_path = {}, {}
    _flatten(data.get("blockdevices") or [], by_name, by_path)
    return by_name, by_path


def whole_disk_of(devpath: str, by_path: dict, by_name: dict) -> Optional[str]:
    """Iteratively walk lsblk's PKNAME chain from devpath up to the node
    with TYPE=disk. A single hop is only correct for a plain partition;
    an LVM logical volume or dm-mapper root needs multiple hops (lv ->
    pv-partition -> disk), so this walks until there's no parent left."""
    node = by_path.get(devpath)
    if node is None:
        return None
    seen = set()
    while node.get("pkname") and node["pkname"] not in seen:
        seen.add(node["pkname"])
        parent = by_name.get(node["pkname"])
        if parent is None:
            break
        node = parent
    return node.get("path") or (f"/dev/{node['name']}" if node.get("name") else None)


def excluded_devices(by_path: dict, by_name: dict):
    """Returns (excluded_whole_disk_paths, ok). ok=False means resolution
    of the ROOT mount failed -- the caller MUST treat this as "exclude
    every device" (fail closed), never as "no exclusions needed"."""
    excluded = set()
    for mp in _BOOT_MOUNTPOINTS:
        try:
            r = subprocess.run(["findmnt", "-no", "SOURCE", mp],
                                capture_output=True, text=True, timeout=5)
            src = (r.stdout or "").strip()
        except Exception:
            src = ""
            r = None
        if not src or r is None or r.returncode != 0:
            if mp == "/":
                return set(), False
            continue
        real = os.path.realpath(src)
        whole = whole_disk_of(real, by_path, by_name)
        if whole is None:
            if mp == "/":
                return set(), False
            continue
        excluded.add(whole)
    return excluded, True


def _swap_device_paths() -> set:
    paths = set()
    try:
        with open("/proc/swaps") as f:
            lines = f.readlines()[1:]
        for line in lines:
            parts = line.split()
            if parts:
                paths.add(parts[0])
                paths.add(os.path.realpath(parts[0]))
    except OSError:
        pass
    return paths


def _fs_in_proc_filesystems(name: str) -> bool:
    try:
        with open("/proc/filesystems") as f:
            return any(name in line for line in f)
    except OSError:
        return False


def f2fs_available() -> bool:
    """Not cached: a re-check is cheap (two lightweight calls) and this is
    only ever invoked when the Depolama page's device list is opened or
    refreshed, not on a tight poll -- so an admin who apt-installs
    f2fs-tools later sees it become available without restarting the
    service."""
    if shutil.which("mkfs.f2fs") is None:
        return False
    if _fs_in_proc_filesystems("f2fs"):
        return True
    try:
        subprocess.run(["modprobe", "f2fs"], capture_output=True, timeout=5)
    except Exception:
        pass
    return _fs_in_proc_filesystems("f2fs")


def list_block_devices() -> dict:
    """Unprivileged device listing. Returns
    {"devices": [...], "error": str|None, "f2fs_available": bool}.

    Only ever returns whole disks with NO partition table (nothing to
    format on top of) and individual partitions -- never a disk that
    already has partitions on it as a single row (formatting that would
    imply destroying its partition table, which is out of scope), and
    never the OS/boot disk or anything under it, or swap.
    """
    f2fs_ok = f2fs_available()
    try:
        by_name, by_path = _run_lsblk()
    except Exception as e:
        return {"devices": [], "error": f"lsblk çalıştırılamadı: {e}", "f2fs_available": f2fs_ok}

    excluded, ok = excluded_devices(by_path, by_name)
    if not ok:
        return {"devices": [], "error": "Sistem diski belirlenemedi — güvenlik için hiçbir disk listelenmiyor.",
                "f2fs_available": f2fs_ok}

    swap_paths = _swap_device_paths()

    out = []
    for path, node in by_path.items():
        ntype = node.get("type")
        if ntype == "disk" and node.get("children"):
            continue  # has partitions -- expose the partitions themselves, not the whole disk
        if ntype not in ("disk", "part"):
            continue
        whole = whole_disk_of(path, by_path, by_name)
        if whole is not None and whole in excluded:
            continue
        if path in swap_paths or os.path.realpath(path) in swap_paths:
            continue
        out.append({
            "path": path,
            "type": ntype,
            "size": int(node.get("size") or 0),
            "fstype": node.get("fstype"),
            "label": node.get("label"),
            "uuid": node.get("uuid"),
            "mountpoint": node.get("mountpoint"),
            "model": node.get("model"),
            "removable": bool(node.get("rm")),
            "readonly": bool(node.get("ro")),
        })
    out.sort(key=lambda d: d["path"])
    return {"devices": out, "error": None, "f2fs_available": f2fs_ok}


# ---------- job queue (unprivileged side: submit + poll) ----------

_pending_by_device: dict = {}


def submit_job(action: str, device_key: str, **payload) -> str:
    """device_key dedups in-memory: format/mount key on the device path,
    unmount keys on the uuid. Belt-and-suspenders against a double-click
    racing two jobs for the same target, on top of whatever the UI does
    to disable buttons while a request is in flight."""
    prev = _pending_by_device.get(device_key)
    if prev is not None and get_job_result(prev) is None:
        raise DiskJobConflict(f"'{device_key}' için zaten bekleyen bir işlem var, sonucunu bekleyin.")
    job_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
    _pending_by_device[device_key] = job_id

    job = {"id": job_id, "action": action, **payload}
    jobs_dir = _jobs_dir()
    # ".part" suffix (not just a differently-prefixed .json name) so the
    # worker's glob("*.json") can NEVER match a still-being-written file,
    # even for the instant between creation and the rename below.
    tmp_path = jobs_dir / f"{job_id}.json.part"
    final_path = jobs_dir / f"{job_id}.json"
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(job, f)
            f.flush()
            os.fsync(f.fileno())
        # Atomic within the same directory/filesystem -- the .path unit's
        # DirectoryNotEmpty= can fire the instant a file is CREATED, so a
        # direct write to the final name risks the root worker reading a
        # truncated/partial body mid-write. rename() has no such window.
        os.rename(tmp_path, final_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        _pending_by_device.pop(device_key, None)
        raise
    return job_id


def get_job_result(job_id: str) -> Optional[dict]:
    path = _results_dir() / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
