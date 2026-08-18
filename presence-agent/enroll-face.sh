#!/usr/bin/env bash
# macOS / Linux 对应 enroll-face.ps1：登记本机本人模板。
set -euo pipefail

AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CAMERA=0
WIDTH=640
HEIGHT=480
SAMPLES=20
DETECTOR_MODEL=""
RECOGNIZER_MODEL=""
TEMPLATE=""
PYTHON_EXE="python3"
FORCE=0

usage() {
    cat <<'USAGE'
用法: enroll-face.sh [选项]
  --camera INDEX            默认 0
  --width PIXELS            默认 640
  --height PIXELS           默认 480
  --samples COUNT           默认 20
  --detector-model PATH     人脸检测模型
  --recognizer-model PATH   人脸识别模型
  --template PATH           模板输出路径，默认 .runtime/owner_template.npz
  --python PATH             建 venv 用的解释器，默认 python3
  --force                   覆盖已存在的模板
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --camera) CAMERA="$2"; shift 2 ;;
        --width) WIDTH="$2"; shift 2 ;;
        --height) HEIGHT="$2"; shift 2 ;;
        --samples) SAMPLES="$2"; shift 2 ;;
        --detector-model) DETECTOR_MODEL="$2"; shift 2 ;;
        --recognizer-model) RECOGNIZER_MODEL="$2"; shift 2 ;;
        --template) TEMPLATE="$2"; shift 2 ;;
        --python) PYTHON_EXE="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

"$AGENT_ROOT/setup.sh" --python "$PYTHON_EXE"

ARGUMENTS=(
    -m presence_agent.face_enrollment
    --camera "$CAMERA"
    --width "$WIDTH"
    --height "$HEIGHT"
    --samples "$SAMPLES"
)
if [ -n "$DETECTOR_MODEL" ]; then ARGUMENTS+=(--detector-model "$DETECTOR_MODEL"); fi
if [ -n "$RECOGNIZER_MODEL" ]; then ARGUMENTS+=(--recognizer-model "$RECOGNIZER_MODEL"); fi
if [ -n "$TEMPLATE" ]; then ARGUMENTS+=(--template "$TEMPLATE"); fi
if [ "$FORCE" -eq 1 ]; then ARGUMENTS+=(--force); fi

cd "$AGENT_ROOT"
set +e
"$AGENT_ROOT/.venv/bin/python" "${ARGUMENTS[@]}"
ENROLLMENT_EXIT_CODE=$?
set -e
if [ "$ENROLLMENT_EXIT_CODE" -ne 0 ]; then
    echo "owner face enrollment failed with exit code $ENROLLMENT_EXIT_CODE" >&2
    exit "$ENROLLMENT_EXIT_CODE"
fi
