#!/usr/bin/env bash
# RtcView installer — Ubuntu Noble (aarch64/rk3399) friendly.
# Creates an isolated Python venv, installs go2rtc, sets up a systemd service.
set -euo pipefail

# ------------- helpers -------------
info(){ printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err(){  printf "\033[1;31m[ERR ]\033[0m %s\n" "$*" >&2; }
die(){  err "$*"; exit 1; }

# ------------- checks -------------
if [ "$(id -u)" -ne 0 ]; then
  die "Bu betiği root olarak çalıştırın (sudo bash install.sh)."
fi

INSTALL_DIR="${RTCVIEW_HOME:-/opt/rtcview}"
SERVICE_USER="rtcview"
SERVICE_NAME="rtcview"
GO2RTC_SERVICE="rtcview-go2rtc"
DEFAULT_PORT=5000

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

read -r -p "Kullanılacak port [${DEFAULT_PORT}]: " USER_PORT
USER_PORT="${USER_PORT:-$DEFAULT_PORT}"
if ! [[ "$USER_PORT" =~ ^[0-9]+$ ]] || [ "$USER_PORT" -lt 1 ] || [ "$USER_PORT" -gt 65535 ]; then
  die "Geçersiz port: $USER_PORT"
fi
if port_in_use "$USER_PORT"; then
  die "Port $USER_PORT şu anda kullanımda. Farklı bir port deneyin."
fi
info "Port $USER_PORT boş — devam ediliyor."

# ------------- packages -------------
info "APT paketleri yükleniyor (python3, venv, curl, ffmpeg)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip curl ca-certificates ffmpeg tar

# ------------- user -------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  info "Servis kullanıcısı ($SERVICE_USER) oluşturuluyor..."
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ------------- directories -------------
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/config"
mkdir -p "$INSTALL_DIR/logs"

# ------------- copy source -------------
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
info "Kaynak kopyalanıyor: $SRC_DIR -> $INSTALL_DIR"
# copy application files (avoid .git, scratch)
tar -C "$SRC_DIR" --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  -cf - app requirements.txt scripts systemd 2>/dev/null | tar -C "$INSTALL_DIR" -xf -

# ------------- venv -------------
info "Python venv oluşturuluyor (izole ortam)..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# ------------- go2rtc -------------
GO2RTC_BIN="$INSTALL_DIR/go2rtc"
if [ ! -x "$GO2RTC_BIN" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64|arm64) GO2RTC_ASSET="go2rtc_linux_arm64" ;;
    armv7l)        GO2RTC_ASSET="go2rtc_linux_arm" ;;
    x86_64|amd64)  GO2RTC_ASSET="go2rtc_linux_amd64" ;;
    *) die "Desteklenmeyen mimari: $ARCH" ;;
  esac
  info "go2rtc indiriliyor ($GO2RTC_ASSET)..."
  URL="https://github.com/AlexxIT/go2rtc/releases/latest/download/${GO2RTC_ASSET}"
  if ! curl -fL "$URL" -o "$GO2RTC_BIN"; then
    warn "go2rtc indirilemedi. Daha sonra manuel olarak ${GO2RTC_BIN} yoluna koyabilirsiniz."
  else
    chmod +x "$GO2RTC_BIN"
  fi
fi

# ------------- write initial config with chosen port -------------
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
  "go2rtc": { "host": "127.0.0.1", "api_port": 1984, "webrtc_port": 8555 },
  "cameras": []
}
JSON
else
  info "Mevcut config bulundu, port güncelleniyor..."
  "$INSTALL_DIR/venv/bin/python" - "$CONFIG_FILE" "$USER_PORT" <<'PY'
import json, sys
p, port = sys.argv[1], int(sys.argv[2])
with open(p) as f: d = json.load(f)
d.setdefault("app", {})["port"] = port
with open(p, "w") as f: json.dump(d, f, indent=2)
PY
fi

# ------------- permissions -------------
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

# ------------- systemd -------------
info "systemd servisi kuruluyor..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=RtcView — go2rtc camera viewer
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
ExecStart=${INSTALL_DIR}/venv/bin/python -m app.main --port ${USER_PORT} --install-dir ${INSTALL_DIR} --config ${CONFIG_FILE}
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=yes
ReadWritePaths=${INSTALL_DIR}
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

# ------------- firewall hint -------------
if command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    info "UFW aktif — port ${USER_PORT}/tcp izinleniyor..."
    ufw allow "${USER_PORT}/tcp" || true
    ufw allow 8555/udp || true
  fi
fi

info "Kurulum tamam. Erişim adresi:"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "  http://${IP:-127.0.0.1}:${USER_PORT}"
echo "Servis: systemctl status ${SERVICE_NAME}"
