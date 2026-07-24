#!/usr/bin/env bash
# RtcView updater — refreshes app code, keeps config & cameras intact.
set -euo pipefail

info(){ printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err(){  printf "\033[1;31m[ERR ]\033[0m %s\n" "$*" >&2; }
die(){  err "$*"; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  die "Bu betiği root olarak çalıştırın (sudo bash update.sh)."
fi

INSTALL_DIR="${RTCVIEW_HOME:-/opt/rtcview}"
SERVICE_USER="rtcview"
SERVICE_NAME="rtcview"

[ -d "$INSTALL_DIR" ] || die "Kurulum bulunamadı: $INSTALL_DIR. Önce install.sh çalıştırın."
[ -x "$INSTALL_DIR/venv/bin/python" ] || die "Venv bozuk. install.sh çalıştırın."

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
info "Kaynak: $SRC_DIR  →  Hedef: $INSTALL_DIR"

# ------------- config backup -------------
CONFIG_FILE="$INSTALL_DIR/config/config.json"
if [ -f "$CONFIG_FILE" ]; then
  BACKUP="/tmp/rtcview-config-$(date +%Y%m%d-%H%M%S).json"
  cp "$CONFIG_FILE" "$BACKUP"
  info "Config yedeği: $BACKUP"
fi

# ------------- stop -------------
info "Servis durduruluyor..."
systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true

# ------------- sync source (config klasörü DOKUNULMAZ) -------------
info "Uygulama dosyaları güncelleniyor..."
# --exclude ile config/ ve venv/ korunur
tar -C "$SRC_DIR" \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='venv' --exclude='config' --exclude='logs' \
  -cf - app requirements.txt scripts 2>/dev/null | \
  tar -C "$INSTALL_DIR" -xf -

# ------------- pip deps (varsa yeni bağımlılık) -------------
info "Python bağımlılıkları kontrol ediliyor..."
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade -r "$INSTALL_DIR/requirements.txt" 2>&1 | tail -3 || \
  warn "pip upgrade sırasında bazı uyarılar oldu — devam ediliyor."

# ------------- permissions -------------
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

# ------------- systemd unit refresh (varsa değişmiş) -------------
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ -f "$UNIT_FILE" ]; then
  # Port'u mevcut config'ten al
  PORT=$("$INSTALL_DIR/venv/bin/python" -c "import json; print(json.load(open('$CONFIG_FILE'))['app']['port'])" 2>/dev/null || echo 5000)
  info "Servis port: $PORT (config'ten okundu)"
fi
systemctl daemon-reload || true

# ------------- restart -------------
info "Servis başlatılıyor..."
systemctl start "${SERVICE_NAME}.service"
sleep 1

if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  info "Güncelleme başarılı — servis çalışıyor."
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "  http://${IP:-127.0.0.1}:${PORT:-5000}"
else
  err "Servis başlatılamadı. Log: journalctl -u ${SERVICE_NAME} -n 40"
  exit 1
fi
