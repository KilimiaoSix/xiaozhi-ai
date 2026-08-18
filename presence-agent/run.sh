#!/usr/bin/env bash
# macOS / Linux 对应 run.ps1：准备 .venv 后启动 presence-agent。
set -euo pipefail

AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVER_URL="http://127.0.0.1:8003"
WORKSTATION_ID=""
CAMERA=0
WIDTH=640
HEIGHT=480
ABSENT_AFTER=2.0
HEARTBEAT_SECONDS=15.0
CAMERA_RETRY_SECONDS=5.0
MODEL=""
FACE_DETECTOR_MODEL=""
FACE_RECOGNIZER_MODEL=""
FACE_TEMPLATE=""
FACE_THRESHOLD=0.45
FACE_HITS=3
NO_FACE_DELAY=1.0
SMOKE_FRAMES=0
PYTHON_EXE="python3"
PREVIEW=0
DISABLE_FACE_VERIFICATION=0

usage() {
    cat <<'USAGE'
用法: run.sh [选项]
  --server-url URL              默认 http://127.0.0.1:8003
  --workstation-id ID           默认取本机 hostname
  --camera INDEX                默认 0
  --width PIXELS                默认 640
  --height PIXELS               默认 480
  --absent-after SECONDS        默认 2.0
  --heartbeat-seconds SECONDS   默认 15.0
  --camera-retry-seconds SEC    默认 5.0
  --model PATH                  姿态模型
  --face-detector-model PATH    人脸检测模型
  --face-recognizer-model PATH  人脸识别模型
  --face-template PATH          本人模板
  --face-threshold SCORE        默认 0.45
  --face-hits COUNT             默认 3
  --no-face-delay SECONDS       默认 1.0
  --smoke-frames COUNT          处理 N 帧后退出，默认 0
  --python PATH                 建 venv 用的解释器，默认 python3
  --preview                     打开本地预览窗口
  --disable-face-verification   只做在岗检测
鉴权令牌只通过环境变量 PRESENCE_AUTH_TOKEN 传入。
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server-url) SERVER_URL="$2"; shift 2 ;;
        --workstation-id) WORKSTATION_ID="$2"; shift 2 ;;
        --camera) CAMERA="$2"; shift 2 ;;
        --width) WIDTH="$2"; shift 2 ;;
        --height) HEIGHT="$2"; shift 2 ;;
        --absent-after) ABSENT_AFTER="$2"; shift 2 ;;
        --heartbeat-seconds) HEARTBEAT_SECONDS="$2"; shift 2 ;;
        --camera-retry-seconds) CAMERA_RETRY_SECONDS="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --face-detector-model) FACE_DETECTOR_MODEL="$2"; shift 2 ;;
        --face-recognizer-model) FACE_RECOGNIZER_MODEL="$2"; shift 2 ;;
        --face-template) FACE_TEMPLATE="$2"; shift 2 ;;
        --face-threshold) FACE_THRESHOLD="$2"; shift 2 ;;
        --face-hits) FACE_HITS="$2"; shift 2 ;;
        --no-face-delay) NO_FACE_DELAY="$2"; shift 2 ;;
        --smoke-frames) SMOKE_FRAMES="$2"; shift 2 ;;
        --python) PYTHON_EXE="$2"; shift 2 ;;
        --preview) PREVIEW=1; shift ;;
        --disable-face-verification) DISABLE_FACE_VERIFICATION=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

"$AGENT_ROOT/setup.sh" --python "$PYTHON_EXE"

if [ -z "$WORKSTATION_ID" ]; then
    WORKSTATION_ID="$(hostname -s 2>/dev/null || hostname)"
fi
NORMALIZED_WORKSTATION_ID="$(printf '%s' "$WORKSTATION_ID" \
    | tr -c 'A-Za-z0-9._-' '-' \
    | sed -e 's/^[-.]*//' -e 's/[-.]*$//' \
    | cut -c1-64)"
if [ -z "$NORMALIZED_WORKSTATION_ID" ]; then
    echo "workstation-id 至少需要一个 [A-Za-z0-9._-] 字符。" >&2
    exit 2
fi

ARGUMENTS=(
    -m presence_agent.app
    --server-url "$SERVER_URL"
    --workstation-id "$NORMALIZED_WORKSTATION_ID"
    --camera "$CAMERA"
    --width "$WIDTH"
    --height "$HEIGHT"
    --absent-after "$ABSENT_AFTER"
    --heartbeat-seconds "$HEARTBEAT_SECONDS"
    --camera-retry-seconds "$CAMERA_RETRY_SECONDS"
    --smoke-frames "$SMOKE_FRAMES"
)
if [ -n "$MODEL" ]; then ARGUMENTS+=(--model "$MODEL"); fi
if [ -n "$FACE_DETECTOR_MODEL" ]; then ARGUMENTS+=(--face-detector-model "$FACE_DETECTOR_MODEL"); fi
if [ -n "$FACE_RECOGNIZER_MODEL" ]; then ARGUMENTS+=(--face-recognizer-model "$FACE_RECOGNIZER_MODEL"); fi
if [ -n "$FACE_TEMPLATE" ]; then ARGUMENTS+=(--face-template "$FACE_TEMPLATE"); fi
ARGUMENTS+=(
    --face-threshold "$FACE_THRESHOLD"
    --face-hits "$FACE_HITS"
    --no-face-delay "$NO_FACE_DELAY"
)
if [ "$DISABLE_FACE_VERIFICATION" -eq 1 ]; then ARGUMENTS+=(--disable-face-verification); fi
if [ "$PREVIEW" -eq 1 ]; then ARGUMENTS+=(--preview); fi

cd "$AGENT_ROOT"
exec "$AGENT_ROOT/.venv/bin/python" "${ARGUMENTS[@]}"
