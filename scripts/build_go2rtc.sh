#!/usr/bin/env bash
# Builds RtcView's patched go2rtc binaries into vendor/go2rtc/.
#
# RtcView bundles its own go2rtc build rather than the stock upstream
# release because of a real, reproducible go2rtc bug: a nil-pointer panic
# in pkg/core/writebuffer.go's Write() that crashes the ENTIRE go2rtc
# process (taking down every camera's stream, not just one) whenever a
# consumer's underlying HTTP response is torn down (client disconnect,
# stream reload) while a Sender goroutine still has a packet in flight.
# Confirmed against upstream issue AlexxIT/go2rtc#1261: three separate
# crash reports share the identical nil-pointer program counter, reached
# via three different codec paths (H264, H265, and PCM/FLAC audio) — this
# is generic to any codec, not something a client-side audio workaround
# can fix. See scripts/go2rtc-writebuffer-recover.patch for the exact
# (small, self-contained) fix: a recover() guard that contains a torn-down
# consumer's stale write as a per-consumer error instead of a process-wide
# panic.
#
# Usage: ./scripts/build_go2rtc.sh [go2rtc git tag]
#   Defaults to the pinned version below. Requires Go 1.24+ and network
#   access to github.com. Output: vendor/go2rtc/go2rtc_linux_<arch> for
#   each arch in TARGETS, plus vendor/go2rtc/VERSION.
#
# To pick up a new upstream go2rtc release later: bump GO2RTC_VERSION,
# re-run this script, confirm scripts/go2rtc-writebuffer-recover.patch
# still applies cleanly (it's one small, stable function — has needed no
# changes across every go2rtc release so far), commit the new binaries.
set -euo pipefail

GO2RTC_VERSION="${1:-v1.9.14}"
REPO_URL="https://github.com/AlexxIT/go2rtc.git"
TARGETS="linux/amd64 linux/arm64"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PATCH_FILE="${SCRIPT_DIR}/go2rtc-writebuffer-recover.patch"
TEST_FILE="${SCRIPT_DIR}/go2rtc-writebuffer-recover_test.go"
OUT_DIR="${ROOT_DIR}/vendor/go2rtc"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

command -v go >/dev/null 2>&1 || { echo "go toolchain not found (need Go 1.21+)" >&2; exit 1; }

echo "Cloning go2rtc ${GO2RTC_VERSION}..."
git clone --depth 1 --branch "${GO2RTC_VERSION}" "${REPO_URL}" "${WORK_DIR}/src" --quiet

REVISION="$(git -C "${WORK_DIR}/src" rev-parse --short HEAD)"

echo "Applying ${PATCH_FILE}..."
git -C "${WORK_DIR}/src" apply "${PATCH_FILE}"

echo "Running regression test for the patch..."
cp "${TEST_FILE}" "${WORK_DIR}/src/pkg/core/go2rtc_rtcview_recover_test.go"
(cd "${WORK_DIR}/src" && go test ./pkg/core/... -run TestWriteBufferRecoversFromWriterPanic -v)

mkdir -p "${OUT_DIR}"

for target in ${TARGETS}; do
  os="${target%/*}"
  arch="${target#*/}"
  out="${OUT_DIR}/go2rtc_${os}_${arch}"
  echo "Building ${target} -> ${out}"
  (
    cd "${WORK_DIR}/src"
    GOOS="${os}" GOARCH="${arch}" CGO_ENABLED=0 go build \
      -trimpath -ldflags="-s -w" -o "${out}" .
  )
  chmod +x "${out}"
done

echo "${GO2RTC_VERSION} (base ${REVISION}, patched: writebuffer.go recover guard, rtcview build 1)" > "${OUT_DIR}/VERSION"

echo "Done. Built for: ${TARGETS}"
ls -la "${OUT_DIR}"
