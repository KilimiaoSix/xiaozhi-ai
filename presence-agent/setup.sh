#!/usr/bin/env bash
# macOS / Linux 对应 setup.ps1：创建 .venv 并按 requirements 哈希增量安装。
set -euo pipefail

AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_EXE="python3"
REQUIREMENTS="requirements.txt"
MIN_PYTHON_MINOR=12

while [ $# -gt 0 ]; do
    case "$1" in
        --python) PYTHON_EXE="$2"; shift 2 ;;
        --include-test) REQUIREMENTS="requirements-test.txt"; shift ;;
        -h|--help)
            echo "用法: setup.sh [--python PATH] [--include-test]"
            exit 0
            ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

VENV_PYTHON="$AGENT_ROOT/.venv/bin/python"
REQUIREMENTS_PATH="$AGENT_ROOT/$REQUIREMENTS"
MARKER_PATH="$AGENT_ROOT/.venv/requirements.sha256"

if [ ! -x "$VENV_PYTHON" ]; then
    if ! command -v "$PYTHON_EXE" >/dev/null 2>&1; then
        echo "找不到 Python: $PYTHON_EXE，使用 --python 指定解释器。" >&2
        exit 2
    fi
    # numpy 2.5.2 要求 Python >= 3.12，用旧解释器建出来的 venv 会在装依赖时才失败。
    if ! "$PYTHON_EXE" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PYTHON_MINOR) else 1)"; then
        echo "presence-agent 需要 Python 3.$MIN_PYTHON_MINOR 或更高版本，当前 $PYTHON_EXE 是 $("$PYTHON_EXE" -V 2>&1)。" >&2
        echo "示例: ./setup.sh --python /opt/homebrew/bin/python3.14" >&2
        exit 2
    fi
    "$PYTHON_EXE" -m venv "$AGENT_ROOT/.venv"
fi

requirements_hash() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$REQUIREMENTS_PATH" | awk '{print $1}'
    else
        sha256sum "$REQUIREMENTS_PATH" | awk '{print $1}'
    fi
}

REQUIREMENTS_HASH="$(requirements_hash)"
INSTALLED_HASH=""
if [ -f "$MARKER_PATH" ]; then
    INSTALLED_HASH="$(tr -d '[:space:]' < "$MARKER_PATH")"
fi

if [ "$INSTALLED_HASH" != "$REQUIREMENTS_HASH" ]; then
    (cd "$AGENT_ROOT" && "$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$REQUIREMENTS")
    printf '%s\n' "$REQUIREMENTS_HASH" > "$MARKER_PATH"
fi
