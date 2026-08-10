"""Watches local network interfaces (ethernet, wifi) for link up/down
transitions and logs them, so a camera dropout can be told apart from a
genuine local network outage after the fact just by reading the log. It
also keeps a live snapshot (current IP, throughput, last up/down times)
and a bounded history of recent events, both served to the UI's "Ağ
Durumu" panel via snapshot() -- this being a network-camera application,
knowing the state of the network itself is treated as first-class, not
an afterthought.

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
import fcntl
import logging
import os
import select
import socket
import struct
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Optional

log = logging.getLogger("rtcview.netmon")

POLL_INTERVAL_SEC = 3
EVENT_HISTORY_MAX = 200
SYS_NET = Path("/sys/class/net")

NETLINK_ROUTE = 0
RTMGRP_LINK = 0x1

SIOCGIFADDR = 0x8915
SIOCGIFHWADDR = 0x8927


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
    """SIOCGIFADDR ioctl instead of shelling out to `ip` -- this is
    called on every poll tick (not just on a transition, so the UI's
    "current IP" stays fresh even on a link that never flaps), and a
    process fork+exec every few seconds per interface is real, avoidable
    overhead on an SBC. Returns None if the interface has no IPv4
    address assigned (also the normal errno for a down interface)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            packed = struct.pack("256s", name[:15].encode())
            res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, packed)
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None


def _mac_of(name: str) -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            packed = struct.pack("256s", name[:15].encode())
            res = fcntl.ioctl(s.fileno(), SIOCGIFHWADDR, packed)
        return ":".join("%02x" % b for b in res[18:24])
    except OSError:
        return None


def _byte_counter(name: str, direction: str) -> Optional[int]:
    try:
        return int((SYS_NET / name / "statistics" / f"{direction}_bytes").read_text().strip())
    except (OSError, ValueError):
        return None


class NetworkMonitor:
    """Background poller: logs whenever a physical interface's link
    comes up (kind + IP) or drops, so `journalctl` alone is enough to
    correlate a camera/recording gap with a real network outage. Also
    keeps a live per-interface snapshot (IP, throughput, last up/down
    times) and a bounded event history for the UI, via snapshot()."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # iface name -> last observed operstate, so only actual
        # transitions get logged, not the state on every poll.
        self._last_state: Dict[str, str] = {}
        # iface name -> full current snapshot dict (see _poll_once).
        self._iface_info: Dict[str, dict] = {}
        # iface name -> (byte counts, monotonic timestamp) from the
        # previous poll, used to derive a rate.
        self._prev_counters: Dict[str, tuple] = {}
        self._events: Deque[dict] = deque(maxlen=EVENT_HISTORY_MAX)
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

    def snapshot(self) -> dict:
        """Thread-safe copy of the current state for the API/UI: every
        tracked interface plus the most recent events, newest first."""
        with self._lock:
            interfaces = sorted(
                (dict(info) for info in self._iface_info.values()),
                key=lambda i: i["name"],
            )
            events = list(reversed(self._events))
        return {"interfaces": interfaces, "events": events}

    def _emit(self, level: int, iface: str, kind: str, event: str, message: str):
        log.log(level, message)
        with self._lock:
            self._events.append({
                "ts": time.time(),
                "iface": iface,
                "kind": kind,
                "event": event,
                "level": logging.getLevelName(level),
                "message": message,
            })

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

        now = time.time()
        now_mono = time.monotonic()
        seen = set()
        for name in names:
            kind = _iface_kind(name)
            if kind is None:
                continue
            seen.add(name)
            state = _operstate(name)
            prev = self._last_state.get(name)
            self._last_state[name] = state
            up = state == "up"
            ip = _ipv4_of(name) if up else None

            rx = _byte_counter(name, "rx")
            tx = _byte_counter(name, "tx")
            rx_rate = tx_rate = 0.0
            prev_counters = self._prev_counters.get(name)
            if prev_counters and rx is not None and tx is not None:
                prev_rx, prev_tx, prev_t = prev_counters
                dt = now_mono - prev_t
                if dt > 0:
                    rx_rate = max(0.0, (rx - prev_rx) / dt)
                    tx_rate = max(0.0, (tx - prev_tx) / dt)
            if rx is not None and tx is not None:
                self._prev_counters[name] = (rx, tx, now_mono)

            with self._lock:
                info = self._iface_info.setdefault(name, {
                    "name": name, "kind": kind,
                    "last_up_at": None, "last_down_at": None,
                })
                info.update({
                    "state": state, "ip": ip, "mac": _mac_of(name),
                    "rx_bytes": rx, "tx_bytes": tx,
                    "rx_rate": rx_rate, "tx_rate": tx_rate,
                })

            if prev is None:
                # First observation this run: log the baseline once so
                # the log always shows what was connected at startup,
                # without treating "first seen == down" as a fresh drop.
                if up:
                    with self._lock:
                        info["last_up_at"] = now
                    self._emit(logging.INFO, name, kind, "up",
                               f"[ağ] {name} ({kind}) bağlı" + (f", IP: {ip}" if ip else ""))
                else:
                    self._emit(logging.INFO, name, kind, "baseline_down",
                               f"[ağ] {name} ({kind}) bağlı değil (durum: {state})")
                continue

            if state == prev:
                continue

            if up:
                with self._lock:
                    info["last_up_at"] = now
                self._emit(logging.INFO, name, kind, "up",
                           f"[ağ] {name} ({kind}) bağlantı kuruldu" + (f", IP: {ip}" if ip else ""))
            elif prev == "up":
                with self._lock:
                    info["last_down_at"] = now
                self._emit(logging.WARNING, name, kind, "down",
                           f"[ağ] {name} ({kind}) bağlantı koptu (durum: {state})")
            else:
                self._emit(logging.INFO, name, kind, "state",
                           f"[ağ] {name} ({kind}) durum değişti: {prev} -> {state}")

        # An interface that vanished entirely (USB wifi dongle unplugged,
        # ...) is also worth a line; drop it so it re-baselines cleanly
        # if it comes back.
        for name in set(self._last_state) - seen:
            kind = self._iface_info.get(name, {}).get("kind", "?")
            self._emit(logging.WARNING, name, kind, "vanished", f"[ağ] arayüz kayboldu: {name}")
            del self._last_state[name]
            self._prev_counters.pop(name, None)
            with self._lock:
                self._iface_info.pop(name, None)
