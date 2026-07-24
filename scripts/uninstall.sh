#!/usr/bin/env bash
# RtcView uninstaller
set -euo pipefail

info(){ printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
die(){  printf "\033[1;31m[ERR ]\033[0m %s\n" "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  die "Bu betiği root olarak çalıştırın (sudo bash uninstall.sh)."
fi

INSTALL_DIR="${RTCVIEW_HOME:-/opt/rtcview}"
SERVICE_USER="rtcview"
SERVICE_NAME="rtcview"

KEEP_CONFIG=0
for a in "$@"; do
  case "$a" in
    --keep-config) KEEP_CONFIG=1 ;;
    *) ;;
  esac
done

info "Servis durduruluyor ve devre dışı bırakılıyor..."
systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload || true

if [ "$KEEP_CONFIG" -eq 1 ] && [ -d "$INSTALL_DIR/config" ]; then
  BACKUP="/root/rtcview-config-$(date +%s).tar.gz"
  info "Config yedekleniyor -> $BACKUP"
  tar -C "$INSTALL_DIR" -czf "$BACKUP" config 2>/dev/null || true
fi

if [ -d "$INSTALL_DIR" ]; then
  info "Kurulum dizini siliniyor: $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
fi

if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  info "Servis kullanıcısı ($SERVICE_USER) siliniyor..."
  userdel "$SERVICE_USER" 2>/dev/null || true
fi

info "Kaldırma tamam."
