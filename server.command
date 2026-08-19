#!/bin/zsh

set -u

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

ROOT_DIR="${0:A:h}"
APP_DIR="${ROOT_DIR}/server/main/xiaozhi-server"
PYTHON="${APP_DIR}/.venv/bin/python"
SERVER_ENTRY="${APP_DIR}/app.py"
RUNTIME_DIR="${APP_DIR}/tmp/launcher"
PID_FILE="${RUNTIME_DIR}/server.pid"
LOG_FILE="${RUNTIME_DIR}/server.log"
LAUNCH_AGENT_FILE="${RUNTIME_DIR}/com.launchcrush.server.plist"
LAUNCH_LABEL="com.launchcrush.server"
LAUNCH_DOMAIN="gui/$(id -u)"
WS_PORT="${XIAOFEI_WS_PORT:-8000}"
HTTP_PORT="${XIAOFEI_HTTP_PORT:-8003}"

info() {
  printf '\033[1;34m[Server]\033[0m %s\n' "$1"
}

success() {
  printf '\033[1;32m[Server]\033[0m %s\n' "$1"
}

fail() {
  printf '\033[1;31m[Server]\033[0m %s\n' "$1" >&2
  return 1
}

read_pid() {
  if [[ -f "${PID_FILE}" ]]; then
    local saved_pid
    saved_pid="$(<"${PID_FILE}")"
    if [[ "${saved_pid}" == <-> ]] && is_server_process "${saved_pid}"; then
      printf '%s' "${saved_pid}"
      return 0
    fi
    rm -f "${PID_FILE}"
  fi

  local absolute_pid
  absolute_pid="$(pgrep -f "${SERVER_ENTRY}" 2>/dev/null | head -n 1)"
  if [[ -n "${absolute_pid}" ]] && is_server_process "${absolute_pid}"; then
    printf '%s' "${absolute_pid}"
    return 0
  fi

  local listener_pid
  listener_pid="$(port_owner "${WS_PORT}")"
  if [[ -n "${listener_pid}" ]] && is_server_process "${listener_pid}"; then
    printf '%s' "${listener_pid}"
  fi
}

launch_job_loaded() {
  launchctl print "${LAUNCH_DOMAIN}/${LAUNCH_LABEL}" >/dev/null 2>&1
}

write_launch_agent() {
  mkdir -p "${RUNTIME_DIR}"
  cat > "${LAUNCH_AGENT_FILE}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${SERVER_ENTRY}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${APP_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${PATH}</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_FILE}</string>
</dict>
</plist>
EOF

  plutil -lint "${LAUNCH_AGENT_FILE}" >/dev/null
}

is_server_process() {
  local pid="$1"
  local command_line
  local working_directory

  kill -0 "${pid}" 2>/dev/null || return 1
  command_line="$(ps -p "${pid}" -o command= 2>/dev/null)"
  if [[ "${command_line}" == *"${SERVER_ENTRY}"* ]]; then
    return 0
  fi

  working_directory="$(lsof -a -p "${pid}" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
  [[ "${working_directory}" == "${APP_DIR}" \
    && "${command_line}" == *".venv/bin/python app.py"* ]]
}

port_owner() {
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -n 1
}

check_port() {
  local port="$1"
  local label="$2"
  local server_pid="${3:-}"
  local owner

  owner="$(port_owner "${port}")"
  if [[ -n "${owner}" && "${owner}" != "${server_pid}" ]]; then
    fail "${label} 端口 ${port} 已被 PID ${owner} 占用。"
    return 1
  fi
}

doctor() {
  local errors=0
  local server_pid

  info "检查完整 Server 运行环境"

  if [[ ! -x "${PYTHON}" ]]; then
    fail "缺少 Python 虚拟环境：${PYTHON}"
    printf '  先执行：cd "%s" && python3.10 -m venv .venv\n' "${APP_DIR}"
    errors=$((errors + 1))
  else
    local version
    version="$(${PYTHON} -c 'import platform; print(platform.python_version())' 2>/dev/null)"
    if [[ "${version}" != 3.10.* ]]; then
      fail "Server 必须使用 Python 3.10，当前为 ${version:-未知版本}。"
      errors=$((errors + 1))
    else
      success "Python ${version}"
    fi

    if (cd "${APP_DIR}" && "${PYTHON}" -c 'import opuslib_next' >/dev/null 2>&1); then
      success "Opus Python 包与原生库"
    else
      fail "找不到可用的 Opus 原生库。"
      printf '  macOS 修复：brew install opus\n'
      errors=$((errors + 1))
    fi
  fi

  if command -v ffmpeg >/dev/null 2>&1; then
    success "FFmpeg $(ffmpeg -version 2>/dev/null | head -n 1 | awk '{print $3}')"
  else
    fail "找不到 FFmpeg。"
    printf '  macOS 修复：brew install ffmpeg\n'
    errors=$((errors + 1))
  fi

  if [[ -f "${APP_DIR}/config.yaml" ]]; then
    success "基础配置文件"
  else
    fail "缺少 ${APP_DIR}/config.yaml"
    errors=$((errors + 1))
  fi

  server_pid="$(read_pid)"
  if [[ -n "${server_pid}" ]] && ! is_server_process "${server_pid}"; then
    server_pid=""
  fi
  check_port "${WS_PORT}" "WebSocket" "${server_pid}" || errors=$((errors + 1))
  check_port "${HTTP_PORT}" "HTTP" "${server_pid}" || errors=$((errors + 1))

  if (( errors > 0 )); then
    fail "环境检查失败，共 ${errors} 项。"
    return 1
  fi

  success "环境检查通过"
}

start_server() {
  local pid
  local attempt

  pid="$(read_pid)"
  if [[ -n "${pid}" ]] && is_server_process "${pid}"; then
    success "已经运行，PID ${pid}"
    status_server
    return 0
  fi

  doctor || return 1
  write_launch_agent || return 1
  : > "${LOG_FILE}"
  rm -f "${PID_FILE}"

  if launch_job_loaded; then
    launchctl bootout "${LAUNCH_DOMAIN}/${LAUNCH_LABEL}" >/dev/null 2>&1 || true
  fi

  info "启动完整 Server"
  if ! launchctl bootstrap "${LAUNCH_DOMAIN}" "${LAUNCH_AGENT_FILE}"; then
    fail "无法注册 macOS LaunchAgent。"
    return 1
  fi

  for attempt in {1..60}; do
    pid="$(pgrep -f "${SERVER_ENTRY}" 2>/dev/null | head -n 1)"
    if [[ -z "${pid}" ]] || ! is_server_process "${pid}"; then
      if launch_job_loaded; then
        sleep 0.5
        continue
      fi
      fail "Server 启动失败，最近日志："
      tail -n 30 "${LOG_FILE}" >&2
      return 1
    fi

    if [[ "$(port_owner "${WS_PORT}")" == "${pid}" \
      && "$(port_owner "${HTTP_PORT}")" == "${pid}" ]]; then
      printf '%s\n' "${pid}" > "${PID_FILE}"
      success "启动完成，PID ${pid}"
      status_server
      return 0
    fi
    sleep 0.5
  done

  fail "Server 在 30 秒内没有监听 ${WS_PORT}/${HTTP_PORT}，请查看：${LOG_FILE}"
}

stop_server() {
  local pid
  local attempt

  pid="$(read_pid)"
  if [[ -z "${pid}" ]] || ! is_server_process "${pid}"; then
    if launch_job_loaded; then
      launchctl bootout "${LAUNCH_DOMAIN}/${LAUNCH_LABEL}" >/dev/null 2>&1 || true
    fi
    rm -f "${PID_FILE}"
    info "Server 当前未运行"
    return 0
  fi

  info "停止 Server，PID ${pid}"
  if launch_job_loaded; then
    launchctl bootout "${LAUNCH_DOMAIN}/${LAUNCH_LABEL}"
  else
    kill -TERM "${pid}"
  fi
  for attempt in {1..50}; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      rm -f "${PID_FILE}"
      success "Server 已停止"
      return 0
    fi
    sleep 0.2
  done

  fail "Server 未在 10 秒内退出，请检查 PID ${pid}。"
}

status_server() {
  local pid
  local ws_owner
  local http_owner

  pid="$(read_pid)"
  ws_owner="$(port_owner "${WS_PORT}")"
  http_owner="$(port_owner "${HTTP_PORT}")"

  if [[ -n "${pid}" ]] && is_server_process "${pid}"; then
    success "运行中，PID ${pid}"
    printf '  WebSocket : ws://127.0.0.1:%s/xiaozhi/v1/ %s\n' \
      "${WS_PORT}" "$([[ "${ws_owner}" == "${pid}" ]] && printf '✓' || printf '等待中')"
    printf '  HTTP      : http://127.0.0.1:%s %s\n' \
      "${HTTP_PORT}" "$([[ "${http_owner}" == "${pid}" ]] && printf '✓' || printf '等待中')"
    printf '  日志      : %s\n' "${LOG_FILE}"
    return 0
  fi

  info "未运行"
  [[ -n "${ws_owner}" ]] && printf '  警告：端口 %s 被 PID %s 占用\n' "${WS_PORT}" "${ws_owner}"
  [[ -n "${http_owner}" ]] && printf '  警告：端口 %s 被 PID %s 占用\n' "${HTTP_PORT}" "${http_owner}"
  return 1
}

show_logs() {
  if [[ ! -f "${LOG_FILE}" ]]; then
    fail "还没有 Server 日志：${LOG_FILE}"
    return 1
  fi
  tail -n 100 -f "${LOG_FILE}"
}

usage() {
  cat <<EOF
用法：./server.command [命令]

  start    启动完整 Server（默认）
  stop     停止 Server
  restart  重启 Server
  status   查看进程、端口和日志位置
  logs     持续查看 Server 日志
  doctor   检查 Python、Opus、FFmpeg、配置和端口
EOF
}

case "${1:-start}" in
  start)
    start_server
    ;;
  stop)
    stop_server
    ;;
  restart)
    stop_server && start_server
    ;;
  status)
    status_server
    ;;
  logs)
    show_logs
    ;;
  doctor)
    doctor
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
