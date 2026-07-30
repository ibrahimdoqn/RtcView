#!/usr/bin/env bash
# RtcView installer — Ubuntu Noble (aarch64/rk3399) friendly.
# Creates an isolated Python venv and installs a systemd service.
# Assumes go2rtc is ALREADY running elsewhere (locally or on your network).
set -euo pipefail

info(){ printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err(){  printf "\033[1;31m[ERR ]\033[0m %s\n" "$*" >&2; }
die(){  err "$*"; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  die "Bu betiği root olarak çalıştırın (sudo bash install.sh)."
fi

INSTALL_DIR="${RTCVIEW_HOME:-/opt/rtcview}"
SERVICE_USER="rtcview"
SERVICE_NAME="rtcview"
DEFAULT_PORT=5000
DEFAULT_G2_HOST="127.0.0.1"
DEFAULT_G2_PORT=1984
DEFAULT_G2_RTSP=8554
DEFAULT_REC_PATH="${INSTALL_DIR}/recordings"

info "RtcView kurulumu başlıyor. Kurulum dizini: ${INSTALL_DIR}"

# ------------- port selection -------------
port_in_use(){
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${p}$"
  else
    (echo >/dev/tcp/127.0.0.1/"$p") >/dev/null 2>&1
  fi
}

read -r -p "RtcView için kullanılacak port [${DEFAULT_PORT}]: " USER_PORT
USER_PORT="${USER_PORT:-$DEFAULT_PORT}"
if ! [[ "$USER_PORT" =~ ^[0-9]+$ ]] || [ "$USER_PORT" -lt 1 ] || [ "$USER_PORT" -gt 65535 ]; then
  die "Geçersiz port: $USER_PORT"
fi
if port_in_use "$USER_PORT"; then
  die "Port $USER_PORT şu anda kullanımda. Farklı bir port deneyin."
fi
info "Port $USER_PORT boş — devam ediliyor."

# ------------- go2rtc endpoint -------------
read -r -p "Mevcut go2rtc API host [${DEFAULT_G2_HOST}]: " G2_HOST
G2_HOST="${G2_HOST:-$DEFAULT_G2_HOST}"
read -r -p "Mevcut go2rtc API port [${DEFAULT_G2_PORT}]: " G2_PORT
G2_PORT="${G2_PORT:-$DEFAULT_G2_PORT}"
if ! [[ "$G2_PORT" =~ ^[0-9]+$ ]]; then die "Geçersiz go2rtc portu: $G2_PORT"; fi
read -r -p "go2rtc RTSP portu (kayıt için) [${DEFAULT_G2_RTSP}]: " G2_RTSP
G2_RTSP="${G2_RTSP:-$DEFAULT_G2_RTSP}"
if ! [[ "$G2_RTSP" =~ ^[0-9]+$ ]]; then die "Geçersiz RTSP portu: $G2_RTSP"; fi

# Best-effort connectivity check (non-fatal)
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 2 "http://${G2_HOST}:${G2_PORT}/api" >/dev/null 2>&1; then
    info "go2rtc erişilebilir: http://${G2_HOST}:${G2_PORT}"
  else
    warn "go2rtc'ye şu an ulaşılamıyor: http://${G2_HOST}:${G2_PORT} (kuruluma devam ediliyor)"
  fi
fi

# ------------- recording path -------------
echo
info "Kayıt dosyaları için bir dizin seçin. Boş bırakırsanız varsayılan kullanılır."
info "Örnek: /mnt/nas/rtcview, /media/usb/rtcview, /srv/rtcview, veya varsayılan."
read -r -p "Kayıt klasörü [${DEFAULT_REC_PATH}]: " REC_PATH
REC_PATH="${REC_PATH:-$DEFAULT_REC_PATH}"
# Absolute path required
case "$REC_PATH" in
  /*) : ;;
  *)  die "Kayıt yolu mutlak olmalı: $REC_PATH" ;;
esac

# ------------- packages -------------
info "APT paketleri yükleniyor (python3, venv, ffmpeg, lxml derleme bağımlılıkları)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  build-essential libxml2-dev libxslt1-dev libffi-dev \
  curl ca-certificates tar ffmpeg

if ! command -v ffmpeg >/dev/null 2>&1; then
  warn "ffmpeg bulunamadı. Kayıt çalışmayacak — kurun: sudo apt-get install ffmpeg"
else
  info "ffmpeg mevcut: $(ffmpeg -version 2>/dev/null | head -1)"
fi

# ------------- user -------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  info "Servis kullanıcısı ($SERVICE_USER) oluşturuluyor..."
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# Grant read access to systemd journal so the UI's log viewer works
if getent group systemd-journal >/dev/null 2>&1; then
  usermod -aG systemd-journal "$SERVICE_USER" 2>/dev/null || true
  info "Servis kullanıcısı systemd-journal grubuna eklendi (UI log görüntüleyicisi için)."
fi

# ------------- directories -------------
mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/config" "$INSTALL_DIR/logs"
mkdir -p "$REC_PATH"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$REC_PATH" || warn "Kayıt klasörü sahipliği güncellenemedi (mount noyptions?)"

# Verify recording path is writable by service user (best-effort)
if ! su -s /bin/sh -c "test -w '$REC_PATH'" "$SERVICE_USER" 2>/dev/null; then
  warn "Kayıt klasörü servis kullanıcısı için yazılabilir görünmüyor: $REC_PATH"
  warn "Mount seçeneklerinizi (uid/gid) veya izinleri kontrol edin."
fi

# ------------- copy source -------------
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
info "Kaynak kopyalanıyor: $SRC_DIR -> $INSTALL_DIR"
tar -C "$SRC_DIR" --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  -cf - app tapo_detector requirements.txt scripts 2>/dev/null | tar -C "$INSTALL_DIR" -xf -

# ------------- venv -------------
info "Python venv oluşturuluyor (izole ortam)..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel setuptools
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# ------------- verify critical imports -------------
info "Python bağımlılıkları doğrulanıyor..."
if ! "$INSTALL_DIR/venv/bin/python" -c "import flask, requests, waitress" 2>/dev/null; then
  die "Flask/requests/waitress import başarısız — kurulum yarıda kesildi."
fi
if ! "$INSTALL_DIR/venv/bin/python" -c "from onvif import ONVIFCamera" 2>/dev/null; then
  warn "onvif-zeep import edilemiyor. PTZ özellikleri devre dışı kalır. Manuel dene:"
  warn "  sudo $INSTALL_DIR/venv/bin/pip install --force-reinstall onvif-zeep zeep lxml"
else
  info "ONVIF/PTZ desteği aktif."
fi

# ------------- initial config -------------
CONFIG_FILE="$INSTALL_DIR/config/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
  cat > "$CONFIG_FILE" <<JSON
{
  "app": {
    "port": ${USER_PORT},
    "host": "0.0.0.0",
    "grid_columns": 3,
    "theme": "dark",
    "show_camera_names": true,
    "show_status_badges": true,
    "auto_reconnect": true,
    "reconnect_delay_ms": 3000
  },
  "go2rtc": { "host": "${G2_HOST}", "api_port": ${G2_PORT}, "rtsp_port": ${G2_RTSP} },
  "recording": {
    "enabled": true,
    "storage_path": "${REC_PATH}",
    "storage_paths": [{"path": "${REC_PATH}", "max_gb": 0}],
    "segment_seconds": 300,
    "retention_days": 14,
    "max_gb": 0,
    "purge_interval_seconds": 60,
    "ffmpeg_path": "ffmpeg"
  },
  "cameras": []
}
JSON
else
  info "Mevcut config bulundu, port ve go2rtc adresi güncelleniyor..."
  "$INSTALL_DIR/venv/bin/python" - "$CONFIG_FILE" "$USER_PORT" "$G2_HOST" "$G2_PORT" "$G2_RTSP" "$REC_PATH" <<'PY'
import json, sys
p, port, host, gport, grtsp, recpath = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]
with open(p) as f: d = json.load(f)
d.setdefault("app", {})["port"] = port
d.setdefault("go2rtc", {}).update({"host": host, "api_port": gport, "rtsp_port": grtsp})
rec = d.setdefault("recording", {})
rec.setdefault("enabled", True)
rec["storage_path"] = recpath
# Normalize storage_paths into the {path, max_gb} object schema.
default_quota = int(rec.get("max_gb", 0) or 0)
paths = rec.get("storage_paths") or [recpath]
migrated = []
for e in paths:
    if isinstance(e, str):
        migrated.append({"path": e, "max_gb": default_quota})
    elif isinstance(e, dict) and e.get("path"):
        migrated.append({"path": str(e["path"]), "max_gb": int(e.get("max_gb", default_quota) or 0)})
rec["storage_paths"] = migrated or [{"path": recpath, "max_gb": 0}]
rec.setdefault("segment_seconds", 300)
rec.setdefault("retention_days", 14)
rec.setdefault("max_gb", 0)
rec.setdefault("purge_interval_seconds", 60)
rec.setdefault("ffmpeg_path", "ffmpeg")
with open(p, "w") as f: json.dump(d, f, indent=2)
PY
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

# ------------- self-update capability (path-triggered root service) -------------
# The main service below runs with NoNewPrivileges=yes — a deliberate
# hardening choice that also means it can NEVER sudo out of itself, so
# self-update can't ask the app to escalate its own privileges. Instead
# the (unprivileged) app just touches a trigger FILE inside its own
# writable INSTALL_DIR — no privilege boundary crossed there at all —
# and this separate .path unit (root, its own unit/cgroup, unrelated to
# rtcview.service) notices the file and runs self_update.sh. Regenerated
# on every install/update since it bakes in the resolved INSTALL_DIR.
info "Kendi kendini güncelleme yetkisi kuruluyor (path-tetiklemeli servis)..."
chmod 0755 "${INSTALL_DIR}/scripts/self_update.sh" 2>/dev/null || true
# Cleanup from the earlier sudo-based design (incompatible with
# NoNewPrivileges=yes, replaced by the path-unit approach below).
rm -f "${INSTALL_DIR}/scripts/trigger_update.sh"
rm -f /etc/sudoers.d/rtcview-selfupdate

cat > "/etc/systemd/system/${SERVICE_NAME}-updater.path" <<PATHUNIT
[Unit]
Description=RtcView self-update trigger watcher

[Path]
PathExists=${INSTALL_DIR}/update.trigger
Unit=${SERVICE_NAME}-updater.service

[Install]
WantedBy=multi-user.target
PATHUNIT

cat > "/etc/systemd/system/${SERVICE_NAME}-updater.service" <<SVCUNIT
[Unit]
Description=RtcView self-update (triggered by update.trigger)

[Service]
Type=oneshot
ExecStart=${INSTALL_DIR}/scripts/self_update.sh
ExecStartPost=-/usr/bin/rm -f ${INSTALL_DIR}/update.trigger
SVCUNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}-updater.path"

# ------------- systemd -------------
info "systemd servisi kuruluyor..."
# ReadWritePaths includes INSTALL_DIR, the chosen REC_PATH, and common mount
# roots (/mnt /media /srv /var/lib) so the user can later switch to any of
# those from the UI without needing a systemd unit change.
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=RtcView — go2rtc camera viewer + recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=RTCVIEW_HOME=${INSTALL_DIR}
Environment=RTCVIEW_CONFIG=${INSTALL_DIR}/config
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/venv/bin/python -m app.main --port ${USER_PORT} --config ${CONFIG_FILE}
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=no
ReadWritePaths=${INSTALL_DIR} ${REC_PATH} /mnt /media /srv /var/lib
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

# ------------- helper CLI: switch recording path (updates unit drop-in) -------------
HELPER=/usr/local/sbin/rtcview-set-recording-path
cat > "$HELPER" <<'SH'
#!/usr/bin/env bash
# Adds an extra path to the RtcView systemd sandbox ReadWritePaths.
# Use when your desired recording directory is outside /opt/rtcview, /mnt,
# /media, /srv, /var/lib (the pre-authorised paths).
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "root gerekli"; exit 1; fi
[ -z "${1:-}" ] && { echo "kullanım: $0 <mutlak-yol>"; exit 2; }
case "$1" in /*) : ;; *) echo "mutlak yol gerekli"; exit 2 ;; esac
mkdir -p "$1"
chown -R rtcview:rtcview "$1" || true
mkdir -p /etc/systemd/system/rtcview.service.d
cat > /etc/systemd/system/rtcview.service.d/extra-paths.conf <<EOF
[Service]
ReadWritePaths=$1
EOF
systemctl daemon-reload
systemctl restart rtcview
echo "Eklendi: $1 (servis yeniden başlatıldı)"
SH
chmod +x "$HELPER"

# ------------- firewall hint -------------
if command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    info "UFW aktif — port ${USER_PORT}/tcp izinleniyor..."
    ufw allow "${USER_PORT}/tcp" || true
  fi
fi

info "Kurulum tamam. Erişim adresi:"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "  http://${IP:-127.0.0.1}:${USER_PORT}"
echo "go2rtc backend: http://${G2_HOST}:${G2_PORT} (RTSP :${G2_RTSP})"
echo "Kayıt klasörü: ${REC_PATH}"
echo "Servis: systemctl status ${SERVICE_NAME}"
echo
echo "İpucu: Kayıt klasörünü /mnt, /media, /srv veya /var/lib altına"
echo "koyarsanız UI'dan değişiklik yeterli. Başka bir yol için:"
echo "  sudo rtcview-set-recording-path /istediginiz/yol"
