"""Watches local network interfaces (ethernet, wifi) for link up/down
transitions and logs them, so a camera dropout can be told apart from a
genuine local network outage after the fact just by reading the log.

Reads everything from /sys/class/net -- no extra dependency (psutil,
etc.) for state, matching the rest of the codebase's /proc-and-/sys-first
convention (see recorder.py's _proc_rss_mb). Interfaces are discovered
dynamically on every poll, never hardcoded by name: a board may have any
number of ethernet ports (the NanoPi R4S ships two), any number of wifi
adapters (usually zero or one), or none of either at all -- the set
handled adapts to whatever hardware is actually present.

State is re-checked on a plain POLL_INTERVAL_SEC timer, but also
immediately whenever the kernel reports a link-state change over a
NETLINK_ROUTE socket (RTMGRP_LINK). The timer alone isn't enough: a
cable that's pulled and replugged (or a wifi association that drops and
recovers) inside one poll window would otherwise look, from two 3-second
snapshots, exactly like nothing happened. Netlink notifications close
that gap -- each kernel event wakes the loop right away, so a blip of a
few seconds still produces a down-then-up pair in the log instead of
silence.
"""
import logging
import os
import re
import select
import socket
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("rtcview.netmon")

POLL_INTERVAL_SEC = 3
SYS_NET = Path("/sys/class/net")

NETLINK_ROUTE = 0
RTMGRP_LINK = 0x1


def _open_netlink() -> Optional[socket.socket]:
    """Best-effort NETLINK_ROUTE listener for link up/down events.
    Returns None if netlink isn't available (e.g. a restrictive
    container/sandbox) -- callers fall back to plain polling."""
    try:
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE)
        s.bind((0, RTMGRP_LINK))
        s.setblocking(False)
        return s
    except OSError:
        return None


# Virtual interfaces that are never a physical link worth reporting
# outages for (loopback, container bridges, VPN tunnels, ...).
_SKIP_PREFIXES = ("lo", "veth", "docker", "br-", "virbr", "tun", "tap",
                   "wg", "tailscale", "ifb", "dummy")


def _iface_kind(name: str) -> Optional[str]:
    """'ethernet' / 'wifi', or None if this isn't a physical NIC worth
    watching (virtual bridge, loopback, tunnel, ...)."""
    if name.startswith(_SKIP_PREFIXES):
        return None
    base = SYS_NET / name
    if (base / "wireless").is_dir():
        return "wifi"
    # A real NIC has a backing device; software bridges/veths/tunnels
    # normally don't.
    if not (base / "device").exists():
        return None
    try:
        # ARPHRD_ETHER == 1 -- covers onboard ethernet and USB dongles.
        if (base / "type").read_text().strip() != "1":
            return None
    except OSError:
        return None
    return "ethernet"


def _operstate(name: str) -> str:
    try:
        return (SYS_NET / name / "operstate").read_text().strip()
    except OSError:
        return "unknown"


def _ipv4_of(name: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", name],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


class NetworkMonitor:
    """Background poller: logs whenever a physical interface's link
    comes up (kind + IP) or drops, so `journalctl` alone is enough to
    correlate a camera/recording gap with a real network outage."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # iface name -> last observed operstate, so only actual
        # transitions get logged, not the state on every poll.
        self._last_state: Dict[str, str] = {}
        # Self-pipe so stop() can wake a blocked select() immediately
        # instead of waiting out the rest of the current poll interval.
        # Opened fresh in start() (not __init__) so a stop()+start()
        # reuse of the same instance never selects on already-closed fds.
        self._wake_r: Optional[int] = None
        self._wake_w: Optional[int] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rtcview-netmon")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        nl_sock = _open_netlink()
        if nl_sock is None:
            log.info("[ağ] netlink olayları dinlenemedi, yalnızca %ss aralıklı "
                      "yoklama kullanılacak (ani kopmalar geç fark edilebilir)",
                      POLL_INTERVAL_SEC)
        try:
            while not self._stop.is_set():
                try:
                    self._poll_once()
                except Exception:
                    log.exception("ağ izleme turu başarısız")

                rlist = [self._wake_r]
                if nl_sock is not None:
                    rlist.append(nl_sock)
                try:
                    ready, _, _ = select.select(rlist, [], [], POLL_INTERVAL_SEC)
                except OSError:
                    ready = []

                if nl_sock is not None and nl_sock in ready:
                    # Consume exactly one queued message, then loop
                    # straight back to _poll_once() -- deliberately NOT
                    # draining everything queued first. If more than one
                    # event is pending (e.g. down immediately followed by
                    # up), each is handled in its own iteration so both
                    # transitions get observed and logged, instead of
                    # collapsing into "no change" by the time we look.
                    try:
                        nl_sock.recv(4096)
                    except OSError:
                        pass
        finally:
            if nl_sock is not None:
                nl_sock.close()
            for fd in (self._wake_r, self._wake_w):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._wake_r = self._wake_w = None

    def _poll_once(self):
        try:
            names = sorted(p.name for p in SYS_NET.iterdir())
        except OSError:
            return

        seen = set()
        for name in names:
            kind = _iface_kind(name)
            if kind is None:
                continue
            seen.add(name)
            state = _operstate(name)
            prev = self._last_state.get(name)
            self._last_state[name] = state

            if prev is None:
                # First observation this run: log the baseline once so
                # the log always shows what was connected at startup,
                # without treating "first seen == down" as a fresh drop.
                if state == "up":
                    ip = _ipv4_of(name)
                    log.info("[ağ] %s (%s) bağlı%s", name, kind,
                              f", IP: {ip}" if ip else "")
                else:
                    log.info("[ağ] %s (%s) bağlı değil (durum: %s)", name, kind, state)
                continue

            if state == prev:
                continue

            if state == "up":
                ip = _ipv4_of(name)
                log.info("[ağ] %s (%s) bağlantı kuruldu%s", name, kind,
                          f", IP: {ip}" if ip else "")
            elif prev == "up":
                log.warning("[ağ] %s (%s) bağlantı koptu (durum: %s)", name, kind, state)
            else:
                log.info("[ağ] %s (%s) durum değişti: %s -> %s", name, kind, prev, state)

        # An interface that vanished entirely (USB wifi dongle unplugged,
        # ...) is also worth a line; drop it so it re-baselines cleanly
        # if it comes back.
        for name in set(self._last_state) - seen:
            log.warning("[ağ] arayüz kayboldu: %s", name)
            del self._last_state[name]
