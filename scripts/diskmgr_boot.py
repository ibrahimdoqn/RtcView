#!/usr/bin/env python3
"""Boot-time remount of RtcView-managed disks — no /etc/fstab entries.

Run by rtcview-diskmgr-boot.service (root, oneshot, ordered strictly
before rtcview.service via Before=/After= in install.sh) since disks
mounted by the Depolama page's Disk Yönetimi card are deliberately NOT
added to /etc/fstab — the kernel won't auto-mount them at boot on its
own, so RtcView has to do it itself, every boot, before the main service
starts (recorders need the mountpoint to already exist and be writable).

Reads config.json directly with a bare json.load() — NEVER constructs a
real app.config.ConfigStore here. ConfigStore.__init__()/load() CREATES
and saves config.json from DEFAULT_CONFIG if the file doesn't exist yet;
at early boot (this unit is ordered before the app has ever had a chance
to run) that would leave a root:root-owned config.json inside a directory
owned by rtcview, and the app's own first-ever startup would then fail to
write its own config (rtcview lacking write permission on a root-owned
file). A missing/unreadable config.json here just means "nothing to
remount yet" — not an error.

A disk that's physically absent (unplugged, USB drive not yet reconnected)
is logged and skipped, never fatal — mirrors the app's own existing
tolerance for a storage root disappearing (storage.py's pick_write_root()/
health()). This script's own failure must never block rtcview.service
from starting; Environment/ordering strictness is handled by systemd,
not by this script raising.
"""
import json
import os
import subprocess
import sys

INSTALL_DIR = os.environ.get("RTCVIEW_HOME", "/opt/rtcview")
CONFIG_PATH = os.path.join(os.environ.get("RTCVIEW_CONFIG", os.path.join(INSTALL_DIR, "config")), "config.json")
SERVICE_USER = os.environ.get("RTCVIEW_SERVICE_USER", "rtcview")


def log(msg: str):
    print(f"[diskmgr_boot] {msg}", flush=True)


def load_disks() -> list:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        log(f"config.json okunamadı, atlanıyor: {e}")
        return []
    return data.get("disks") or []


def mount_one(disk: dict):
    disk_uuid = disk.get("uuid")
    mountpoint = disk.get("mountpoint")
    fstype = disk.get("fstype") or "auto"
    if not disk_uuid or not mountpoint or not str(mountpoint).startswith("/"):
        log(f"geçersiz disk kaydı, atlanıyor: {disk}")
        return

    by_uuid = f"/dev/disk/by-uuid/{disk_uuid}"
    if not os.path.exists(by_uuid):
        log(f"disk takılı değil, atlanıyor: uuid={disk_uuid} -> {mountpoint}")
        return

    r = subprocess.run(["findmnt", "-no", "SOURCE", mountpoint], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        log(f"zaten bağlı, atlanıyor: {mountpoint}")
        return

    try:
        os.makedirs(mountpoint, exist_ok=True)
    except OSError as e:
        log(f"mountpoint oluşturulamadı {mountpoint}: {e}")
        return

    r = subprocess.run(["mount", "-t", fstype, by_uuid, mountpoint], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"mount başarısız {mountpoint}: {r.stderr.strip()}")
        return

    r = subprocess.run(["chown", f"{SERVICE_USER}:{SERVICE_USER}", mountpoint], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"chown başarısız {mountpoint}: {r.stderr.strip()}")
        return

    log(f"bağlandı: uuid={disk_uuid} -> {mountpoint} ({fstype})")


def main():
    if os.geteuid() != 0:
        log("root olarak çalıştırılmalı")
        sys.exit(1)
    disks = load_disks()
    if not disks:
        log("yönetilen disk yok, çıkılıyor")
        return
    for d in disks:
        try:
            mount_one(d)
        except Exception as e:
            log(f"beklenmeyen hata ({d.get('uuid')}): {e}")


if __name__ == "__main__":
    main()
