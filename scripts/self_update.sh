#!/usr/bin/env bash
# RtcView self-updater — pulls the latest code from GitHub into a
# persistent source checkout, then hands off to update.sh (the same
# script used for a manual update) for the actual file sync, config
# migration, and service restart.
#
# This is meant to be triggered from inside the running app itself (see
# trigger_update.sh + the "Şimdi Güncelle" button in Ayarlar → Sistem),
# not run directly by hand — for a manual update, just `git pull` your
# own checkout and run update.sh as usual.
set -euo pipefail

info(){ printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err(){  printf "\033[1;31m[ERR ]\033[0m %s\n" "$*" >&2; }
die(){  err "$*"; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  die "Bu betiği root olarak çalıştırın (sudo bash self_update.sh)."
fi

INSTALL_DIR="${RTCVIEW_HOME:-/opt/rtcview}"
# Kept entirely separate from INSTALL_DIR (which update.sh treats as a
# plain file-sync target, not a git working tree) so this git checkout
# can never collide with the live app's own files/permissions.
SRC_DIR="${RTCVIEW_SRC:-${INSTALL_DIR}-src}"
DEFAULT_REPO_URL="https://github.com/ibrahimdoqn/RtcView.git"

if [ ! -d "$SRC_DIR/.git" ]; then
  REPO_URL="${RTCVIEW_REPO_URL:-$DEFAULT_REPO_URL}"
  info "Kaynak kopyası yok, klonlanıyor: $REPO_URL -> $SRC_DIR"
  rm -rf "$SRC_DIR"
  git clone --depth 1 "$REPO_URL" "$SRC_DIR"
else
  # Whatever branch the checkout is already on keeps being tracked — no
  # hardcoded branch name, so this works regardless of which branch was
  # deployed originally.
  BRANCH="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)"
  info "Kaynak güncelleniyor: $SRC_DIR (dal: $BRANCH)"
  git -C "$SRC_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$SRC_DIR" reset --hard "origin/$BRANCH"
fi

info "Sürüm: $(git -C "$SRC_DIR" log -1 --format='%h  %ci  %s')"

[ -x "$SRC_DIR/scripts/update.sh" ] || die "update.sh bulunamadı: $SRC_DIR/scripts/update.sh"
info "update.sh çalıştırılıyor (dosya senkronizasyonu + servis yeniden başlatma)..."
bash "$SRC_DIR/scripts/update.sh"
