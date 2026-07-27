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

# Bazı ffmpeg alt süreçleri systemd cgroup dışına düşebiliyor (nadir de
# olsa oluyor) — yetim kalanları temizle. rtcview kullanıcısına ait olan
# ve INSTALL_DIR/REC_PATH ile bağlantılı ffmpeg'leri hedef alıyoruz;
# başka ffmpeg süreçlerine dokunmuyoruz.
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  ORPHANS=$(pgrep -u "$SERVICE_USER" -f 'ffmpeg .*(-f segment|/mnt/|/opt/rtcview|kayitlar|recordings)' 2>/dev/null || true)
  if [ -n "$ORPHANS" ]; then
    warn "Yetim ffmpeg süreçleri bulundu — sonlandırılıyor: $ORPHANS"
    kill $ORPHANS 2>/dev/null || true
    sleep 2
    STILL=$(pgrep -u "$SERVICE_USER" -f 'ffmpeg .*(-f segment|/mnt/|/opt/rtcview|kayitlar|recordings)' 2>/dev/null || true)
    if [ -n "$STILL" ]; then
      warn "İnatçı süreçler SIGKILL ile durduruluyor: $STILL"
      kill -9 $STILL 2>/dev/null || true
    fi
  fi
fi

# ------------- sync source (config klasörü DOKUNULMAZ) -------------
info "Uygulama dosyaları güncelleniyor..."
tar -C "$SRC_DIR" \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='venv' --exclude='config' --exclude='logs' --exclude='recordings' \
  -cf - app requirements.txt scripts 2>/dev/null | \
  tar -C "$INSTALL_DIR" -xf -

# ------------- pip deps -------------
info "Python bağımlılıkları kontrol ediliyor..."
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade -r "$INSTALL_DIR/requirements.txt" 2>&1 | tail -3 || \
  warn "pip upgrade sırasında bazı uyarılar oldu — devam ediliyor."

# ------------- ffmpeg availability -------------
if ! command -v ffmpeg >/dev/null 2>&1; then
  warn "ffmpeg bulunamadı — kayıt çalışmayacak. Kurulum: sudo apt-get install ffmpeg"
else
  info "ffmpeg mevcut: $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
fi

# ------------- verify onvif -------------
if ! "$INSTALL_DIR/venv/bin/python" -c "from onvif import ONVIFCamera" 2>/dev/null; then
  warn "onvif-zeep hâlâ import edilemiyor. Zorla yeniden kuruluyor..."
  apt-get install -y --no-install-recommends python3-dev build-essential libxml2-dev libxslt1-dev libffi-dev >/dev/null 2>&1 || true
  "$INSTALL_DIR/venv/bin/pip" install --force-reinstall --no-cache-dir onvif-zeep zeep lxml 2>&1 | tail -5
  if "$INSTALL_DIR/venv/bin/python" -c "from onvif import ONVIFCamera" 2>/dev/null; then
    info "ONVIF/PTZ desteği artık aktif."
  else
    warn "ONVIF hâlâ kurulamadı. journalctl -u ${SERVICE_NAME} ile hatayı inceleyin."
  fi
else
  info "ONVIF/PTZ desteği: aktif"
fi

# ------------- config migration: add missing recording block -------------
if [ -f "$CONFIG_FILE" ]; then
  info "Config'e eksik varsayılanlar ekleniyor (recording, rtsp_port)..."
  "$INSTALL_DIR/venv/bin/python" - "$CONFIG_FILE" "$INSTALL_DIR" <<'PY'
import json, os, sys
cfg_path, install_dir = sys.argv[1], sys.argv[2]
with open(cfg_path) as f: d = json.load(f)
d.setdefault("go2rtc", {}).setdefault("rtsp_port", 8554)
rec = d.setdefault("recording", {})
rec.setdefault("enabled", True)
rec.setdefault("storage_path", os.path.join(install_dir, "recordings"))
# Multi-storage schema evolution:
#   old scalar recording.storage_path  →  ["path"]
#   list of strings                    →  [{"path": ..., "max_gb": <legacy max_gb>}]
# The legacy global recording.max_gb becomes the per-disk default for
# the very first migration, then future edits are per-disk from the UI.
if not rec.get("storage_paths"):
    rec["storage_paths"] = [rec["storage_path"]]
default_quota = int(rec.get("max_gb", 0) or 0)
migrated = []
for entry in rec["storage_paths"]:
    if isinstance(entry, str):
        migrated.append({"path": entry, "max_gb": default_quota})
    elif isinstance(entry, dict) and entry.get("path"):
        migrated.append({
            "path": str(entry["path"]),
            "max_gb": int(entry.get("max_gb", default_quota) or 0),
        })
rec["storage_paths"] = migrated or [{"path": rec["storage_path"], "max_gb": 0}]
rec.setdefault("segment_seconds", 300)
rec.setdefault("retention_days", 14)
# Legacy scalar retained for backward compat; UI no longer edits it.
rec.setdefault("max_gb", 0)
rec.setdefault("purge_interval_seconds", 60)
rec.setdefault("ffmpeg_path", "ffmpeg")
for cam in d.get("cameras", []):
    cam.setdefault("record_mode", "off")
    cam.setdefault("record_schedule", [])
    cam.setdefault("record_audio", False)
    cam.setdefault("retention_days_override", 0)
    cam.setdefault("motion_detect_enabled", False)
for _entry in rec["storage_paths"]:
    os.makedirs(_entry["path"], exist_ok=True)
with open(cfg_path, "w") as f: json.dump(d, f, indent=2)
print("recording.storage_paths =", rec["storage_paths"])
PY
fi

# ------------- journal group (for UI log viewer) -------------
if getent group systemd-journal >/dev/null 2>&1 && id -u "$SERVICE_USER" >/dev/null 2>&1; then
  if ! id -nG "$SERVICE_USER" | grep -qw systemd-journal; then
    info "Servis kullanıcısı systemd-journal grubuna ekleniyor (UI log görüntüleyicisi için)..."
    usermod -aG systemd-journal "$SERVICE_USER" 2>/dev/null || true
  fi
fi

# ------------- permissions (recording folder too) -------------
REC_PATH=$("$INSTALL_DIR/venv/bin/python" -c "import json; print(json.load(open('$CONFIG_FILE'))['recording']['storage_path'])" 2>/dev/null || echo "$INSTALL_DIR/recordings")
mkdir -p "$REC_PATH"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$REC_PATH" || warn "Kayıt klasörü sahipliği güncellenemedi: $REC_PATH"

# ------------- systemd unit: ensure recording path + common mounts writable -------------
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ -f "$UNIT_FILE" ]; then
  PORT=$("$INSTALL_DIR/venv/bin/python" -c "import json; print(json.load(open('$CONFIG_FILE'))['app']['port'])" 2>/dev/null || echo 5000)
  info "Servis port: $PORT (config'ten okundu)"
  # If ReadWritePaths does not already mention the recording path, drop-in extra.
  if ! grep -q "ReadWritePaths=.*${REC_PATH}" "$UNIT_FILE"; then
    if ! grep -q "ReadWritePaths=.*\(/mnt\|/media\|/srv\|/var/lib\)" "$UNIT_FILE"; then
      info "Sistemd sandbox güncelleniyor (mount kökleri + kayıt yolu ekleniyor)..."
      mkdir -p /etc/systemd/system/${SERVICE_NAME}.service.d
      cat > /etc/systemd/system/${SERVICE_NAME}.service.d/paths.conf <<EOF
[Service]
ReadWritePaths=${REC_PATH} /mnt /media /srv /var/lib
EOF
    fi
  fi
fi

# systemctl stop komutunun ffmpeg dahil TÜM alt süreçleri kesin olarak
# öldürmesini garanti et: control-group ile SIGTERM, 20 sn sonra SIGKILL.
info "Sistemd kill politikası pekiştiriliyor (control-group + TimeoutStopSec=20)..."
mkdir -p /etc/systemd/system/${SERVICE_NAME}.service.d
cat > /etc/systemd/system/${SERVICE_NAME}.service.d/kill.conf <<'EOF'
[Service]
KillMode=control-group
KillSignal=SIGTERM
SendSIGKILL=yes
TimeoutStopSec=20
EOF
systemctl daemon-reload || true

# Helper CLI (idempotent)
HELPER=/usr/local/sbin/rtcview-set-recording-path
if [ ! -x "$HELPER" ]; then
  cat > "$HELPER" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "root gerekli"; exit 1; fi
[ -z "${1:-}" ] && { echo "kullanım: $0 <mutlak-yol>"; exit 2; }
case "$1" in /*) : ;; *) echo "mutlak yol gerekli"; exit 2 ;; esac
mkdir -p "$1"; chown -R rtcview:rtcview "$1" || true
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
fi

# ------------- restart -------------
info "Servis başlatılıyor..."
systemctl start "${SERVICE_NAME}.service"
sleep 1

if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  info "Güncelleme başarılı — servis çalışıyor."
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "  http://${IP:-127.0.0.1}:${PORT:-5000}"
  echo "  Kayıt klasörü: ${REC_PATH}"
else
  err "Servis başlatılamadı. Log: journalctl -u ${SERVICE_NAME} -n 40"
  exit 1
fi
