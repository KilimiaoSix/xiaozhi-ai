#!/usr/bin/env bash
# macOS / Linux 对应 run-presence-stack.ps1：一键启动 presence Server 与 presence-agent。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ROOT="$REPO_ROOT/presence-agent"
SERVER_ROOT="$REPO_ROOT/server/main/xiaozhi-server"
RUNTIME_ROOT="$AGENT_ROOT/.runtime"
AGENT_PYTHON="$AGENT_ROOT/.venv/bin/python"

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
SERVER_PYTHON=""
PREVIEW=0
ENROLL_OWNER=0
DELETE_FACE_TEMPLATE=0
FORCE_ENROLLMENT=0
DISABLE_FACE_VERIFICATION=0
STARTED_SERVER_PID=""

usage() {
    cat <<'USAGE'
用法: run-presence-stack.sh [选项]
  --server-url URL              默认 http://127.0.0.1:8003
  --workstation-id ID           默认取本机 hostname
  --camera INDEX / --width / --height
  --absent-after / --heartbeat-seconds / --camera-retry-seconds SECONDS
  --model / --face-detector-model / --face-recognizer-model / --face-template PATH
  --face-threshold SCORE        默认 0.45
  --face-hits COUNT             默认 3
  --no-face-delay SECONDS       默认 1.0
  --smoke-frames COUNT          处理 N 帧后退出，默认 0
  --python PATH                 presence-agent venv 解释器，默认 python3
  --server-python PATH          用完整 Server(app.py) 启动时的解释器
  --preview                     打开本地预览窗口
  --enroll-owner                先登记本人再启动
  --force-enrollment            登记时覆盖已有模板
  --delete-face-template        删除本地模板后退出
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
        --server-python) SERVER_PYTHON="$2"; shift 2 ;;
        --preview) PREVIEW=1; shift ;;
        --enroll-owner) ENROLL_OWNER=1; shift ;;
        --force-enrollment) FORCE_ENROLLMENT=1; shift ;;
        --delete-face-template) DELETE_FACE_TEMPLATE=1; shift ;;
        --disable-face-verification) DISABLE_FACE_VERIFICATION=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

cleanup() {
    if [ -n "$STARTED_SERVER_PID" ] && kill -0 "$STARTED_SERVER_PID" 2>/dev/null; then
        kill "$STARTED_SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

resolve_server_endpoint() {
    # 端口写死在 SERVER_URL 里，保证健康检查、Server 绑定和 agent 三方指向同一个地址。
    local url="${SERVER_URL%/}"
    local scheme="${url%%://*}"
    local rest="${url#*://}"
    local hostport="${rest%%/*}"
    local host port
    case "$hostport" in
        \[*\]*)
            host="${hostport%%\]*}"
            host="${host#\[}"
            port="${hostport##*\]}"
            port="${port#:}"
            ;;
        *:*)
            host="${hostport%%:*}"
            port="${hostport##*:}"
            ;;
        *)
            host="$hostport"
            port=""
            ;;
    esac
    if [ -z "$port" ]; then
        if [ "$scheme" = "https" ]; then port=443; else port=80; fi
        SERVER_URL="$scheme://$hostport:$port"
    else
        SERVER_URL="$url"
    fi
    SERVER_HOST="$host"
    SERVER_PORT="$port"
}

presence_server_ready() {
    local url="${SERVER_URL%/}/xiaozhi/presence/__healthcheck__"
    local status
    # macOS 自带 bash 3.2：set -u 下不能展开空数组，所以按有无令牌分成两条调用。
    if [ -n "${PRESENCE_AUTH_TOKEN:-}" ]; then
        status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
            --oauth2-bearer "${PRESENCE_AUTH_TOKEN}" "$url" 2>/dev/null || true)"
    else
        status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
            "$url" 2>/dev/null || true)"
    fi
    case "$status" in
        200|401|404) return 0 ;;
        *) return 1 ;;
    esac
}

wait_presence_server() {
    local deadline=$((SECONDS + 30))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if presence_server_ready; then return 0; fi
        sleep 0.25
    done
    return 1
}

resolve_server_endpoint

RESOLVED_FACE_TEMPLATE="$FACE_TEMPLATE"
if [ -z "$RESOLVED_FACE_TEMPLATE" ]; then
    RESOLVED_FACE_TEMPLATE="$RUNTIME_ROOT/owner_template.npz"
fi

if [ "$DELETE_FACE_TEMPLATE" -eq 1 ]; then
    if [ -f "$RESOLVED_FACE_TEMPLATE" ]; then
        rm -f "$RESOLVED_FACE_TEMPLATE"
        echo "Deleted local face template: $RESOLVED_FACE_TEMPLATE"
    else
        echo "Local face template not found: $RESOLVED_FACE_TEMPLATE"
    fi
    exit 0
fi

"$AGENT_ROOT/setup.sh" --python "$PYTHON_EXE"

if [ "$ENROLL_OWNER" -eq 1 ]; then
    ENROLL_ARGUMENTS=(
        --camera "$CAMERA"
        --width "$WIDTH"
        --height "$HEIGHT"
        --template "$RESOLVED_FACE_TEMPLATE"
        --python "$PYTHON_EXE"
    )
    if [ -n "$FACE_DETECTOR_MODEL" ]; then ENROLL_ARGUMENTS+=(--detector-model "$FACE_DETECTOR_MODEL"); fi
    if [ -n "$FACE_RECOGNIZER_MODEL" ]; then ENROLL_ARGUMENTS+=(--recognizer-model "$FACE_RECOGNIZER_MODEL"); fi
    if [ "$FORCE_ENROLLMENT" -eq 1 ]; then ENROLL_ARGUMENTS+=(--force); fi
    "$AGENT_ROOT/enroll-face.sh" "${ENROLL_ARGUMENTS[@]}"
fi

if ! presence_server_ready; then
    case "$SERVER_HOST" in
        127.0.0.1|localhost|::1) ;;
        *)
            echo "Remote presence Server is unavailable. Start it first or check --server-url." >&2
            exit 1
            ;;
    esac

    mkdir -p "$RUNTIME_ROOT"
    SERVER_STDOUT="$RUNTIME_ROOT/presence-server.out.log"
    SERVER_STDERR="$RUNTIME_ROOT/presence-server.err.log"
    # exec 让子 shell 被 Python 取代，$! 才是真正要收的进程；否则退出时只杀掉外层 shell，
    # Server 会继续占着端口。
    if [ -n "$SERVER_PYTHON" ]; then
        (cd "$SERVER_ROOT" && exec "$SERVER_PYTHON" app.py) \
            > "$SERVER_STDOUT" 2> "$SERVER_STDERR" &
    else
        (cd "$SERVER_ROOT" && exec "$AGENT_PYTHON" presence_server.py \
            --host "$SERVER_HOST" --port "$SERVER_PORT") \
            > "$SERVER_STDOUT" 2> "$SERVER_STDERR" &
    fi
    STARTED_SERVER_PID=$!

    if ! wait_presence_server; then
        echo "Presence Server did not become ready." >&2
        if [ -f "$SERVER_STDERR" ]; then cat "$SERVER_STDERR" >&2; fi
        exit 1
    fi
fi

RUN_ARGUMENTS=(
    --server-url "$SERVER_URL"
    --camera "$CAMERA"
    --width "$WIDTH"
    --height "$HEIGHT"
    --absent-after "$ABSENT_AFTER"
    --heartbeat-seconds "$HEARTBEAT_SECONDS"
    --camera-retry-seconds "$CAMERA_RETRY_SECONDS"
    --smoke-frames "$SMOKE_FRAMES"
    --python "$PYTHON_EXE"
    --face-template "$RESOLVED_FACE_TEMPLATE"
    --face-threshold "$FACE_THRESHOLD"
    --face-hits "$FACE_HITS"
    --no-face-delay "$NO_FACE_DELAY"
)
if [ -n "$WORKSTATION_ID" ]; then RUN_ARGUMENTS+=(--workstation-id "$WORKSTATION_ID"); fi
if [ -n "$MODEL" ]; then RUN_ARGUMENTS+=(--model "$MODEL"); fi
if [ -n "$FACE_DETECTOR_MODEL" ]; then RUN_ARGUMENTS+=(--face-detector-model "$FACE_DETECTOR_MODEL"); fi
if [ -n "$FACE_RECOGNIZER_MODEL" ]; then RUN_ARGUMENTS+=(--face-recognizer-model "$FACE_RECOGNIZER_MODEL"); fi
if [ "$PREVIEW" -eq 1 ]; then RUN_ARGUMENTS+=(--preview); fi
if [ "$DISABLE_FACE_VERIFICATION" -eq 1 ]; then RUN_ARGUMENTS+=(--disable-face-verification); fi

"$AGENT_ROOT/run.sh" "${RUN_ARGUMENTS[@]}"
