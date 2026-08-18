#!/usr/bin/env bash
set -euo pipefail

IDF_DIR="${IDF_DIR:-/Users/kilimiao/esp/esp-idf-v5.5.3}"

if [[ ! -f "$IDF_DIR/export.sh" ]]; then
    echo "FAIL: ESP-IDF not found at $IDF_DIR" >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$IDF_DIR/export.sh" >/dev/null

echo "ESP-IDF: $(idf.py --version)"
echo "esptool: $(python -m esptool version | tail -1)"
echo "Ninja: $(ninja --version)"
echo "CMake: $(cmake --version | head -1)"
echo "Toolchain: $(xtensa-esp32s3-elf-gcc --version | head -1)"

echo "Serial candidates:"
found=0
for pattern in /dev/cu.wchusbserial* /dev/cu.usbmodem* /dev/cu.usbserial*; do
    for port in $pattern; do
        if [[ -e "$port" ]]; then
            echo "  $port"
            found=1
        fi
    done
done

if [[ $found -eq 0 ]]; then
    echo "  none (connect the ESP32-S3 and approve the WCH driver extension if needed)"
fi
