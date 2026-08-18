#!/usr/bin/env bash
set -euo pipefail

IDF_DIR="${IDF_DIR:-/Users/kilimiao/esp/esp-idf-v5.5.3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$PROJECT_DIR/build-macos}"
FLASH_BAUD="${FLASH_BAUD:-460800}"
FACTORY_BIN="${FACTORY_BIN:-/Users/kilimiao/Downloads/小智桌面机器人资料/小智固件2.0.1/xiaozhi-gesture-official-full-80m.bin}"

usage() {
    cat <<'EOF'
Usage:
  scripts/flash.sh <PORT> project
  scripts/flash.sh <PORT> factory --yes

Examples:
  scripts/flash.sh /dev/cu.wchusbserial110 project
  scripts/flash.sh /dev/cu.wchusbserial110 factory --yes

Modes:
  project  Build and flash the current source tree without erasing NVS.
  factory  Flash the complete 16 MB factory image at 0x0. This overwrites NVS.
EOF
}

if [[ $# -lt 2 ]]; then
    usage
    exit 2
fi

PORT="$1"
MODE="$2"

if [[ ! -e "$PORT" ]]; then
    echo "Serial port not found: $PORT" >&2
    echo "Available candidates:" >&2
    ls -1 /dev/cu.wchusbserial* /dev/cu.usbmodem* /dev/cu.usbserial* 2>/dev/null || true
    exit 1
fi

if [[ ! -f "$IDF_DIR/export.sh" ]]; then
    echo "ESP-IDF export script not found: $IDF_DIR/export.sh" >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$IDF_DIR/export.sh" >/dev/null

case "$MODE" in
    project)
        exec idf.py -C "$PROJECT_DIR" -B "$BUILD_DIR" -p "$PORT" -b "$FLASH_BAUD" flash
        ;;
    factory)
        if [[ "${3:-}" != "--yes" ]]; then
            echo "Factory flashing overwrites Wi-Fi and device configuration." >&2
            echo "Re-run with 'factory --yes' only after confirming the target port." >&2
            exit 2
        fi
        if [[ ! -f "$FACTORY_BIN" ]]; then
            echo "Factory image not found: $FACTORY_BIN" >&2
            exit 1
        fi
        exec python -m esptool \
            --chip esp32s3 \
            --port "$PORT" \
            --baud "$FLASH_BAUD" \
            --before default_reset \
            --after hard_reset \
            write_flash \
            --flash_mode dio \
            --flash_freq 80m \
            --flash_size 16MB \
            0x0 "$FACTORY_BIN"
        ;;
    *)
        echo "Unknown flash mode: $MODE" >&2
        usage
        exit 2
        ;;
esac
