#!/usr/bin/env bash
set -euo pipefail

IDF_DIR="${IDF_DIR:-/Users/kilimiao/esp/esp-idf-v5.5.3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$PROJECT_DIR/build-macos}"
BAUD="${MONITOR_BAUD:-115200}"

if [[ $# -ne 1 ]]; then
    echo "Usage: scripts/monitor.sh <PORT>" >&2
    exit 2
fi

PORT="$1"
if [[ ! -e "$PORT" ]]; then
    echo "Serial port not found: $PORT" >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$IDF_DIR/export.sh" >/dev/null

if [[ -f "$BUILD_DIR/xiaozhi.elf" ]]; then
    exec idf.py -C "$PROJECT_DIR" -B "$BUILD_DIR" -p "$PORT" monitor
fi

echo "No macOS build ELF found; starting raw serial monitor at ${BAUD} baud."
exec python -m serial.tools.miniterm "$PORT" "$BAUD" --raw
