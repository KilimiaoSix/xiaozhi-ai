#!/usr/bin/env bash
# 流程四/五「离席 → 来访留言 → 回岗告知」的拍摄用手动触发器。
#
# 为什么要有这个：真链路依赖摄像头姿态判定，而本机取景只拍到头和肩顶，
# 7 个核心关键点常年只可见 3 个（判在岗要 >=4），presence 恒判 absent，
# 到岗事件不触发、返岗汇总永远不播。取景没调好之前，拍摄用这个脚本按拍子走，
# 每一句都是写死的文案，不受识别抖动影响。
#
# 用法：
#   demo-away-return.sh away      # ① 声明会议中 + 小飞进休眠画面（静音）
#   demo-away-return.sh visitor   # ② 同事来访，小飞开口问要不要留言
#   demo-away-return.sh recorded  # ③ 同事留完话，小飞说记下了
#   demo-away-return.sh back      # ④ 主人回来，小飞播欢迎回来 + 离席汇总
#   demo-away-return.sh reset     # 清状态：主人状态回在岗、台账清空、画面回待机
#
# 台词改这里的 TEXT_* 常量即可。back 的文案支持命令行覆盖：
#   demo-away-return.sh back "欢迎回来。你离开的十分钟里，有两条留言。"

set -uo pipefail

SERVER="${DESKPET_SERVER:-http://127.0.0.1:8003}"
DEVICE="${DESKPET_DEVICE_ID:-dc:da:0c:26:9a:60}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEDGER="${DESKPET_LEDGER:-$REPO_ROOT/server/main/xiaozhi-server/data/away_ledger.json}"

# ── 台词（写死，改这里）────────────────────────────────
TEXT_ACK="知道了，我帮你看着工位。"
TEXT_SLEEP="工位没人，我先眯一会儿。"
TEXT_VISITOR="他正在开会，预计11:30回来。需要帮你留句话吗？"
TEXT_RECORDED="记下了，他回来我就提醒。"
TEXT_BACK="欢迎回来。你离开的这半小时里，有同事留言：让你一会儿回来找他。Claude Code 完成了两个任务，都通过了。"

push() {
    # $1=text  $2=emotion  $3=status  $4=speak(true/false)  $5=action(可空)
    # JSON 用 python3 的 json.dumps 生成，不再手拼字符串：back 的自定义文案
    # 一带双引号手拼就非法，之前还静默发出去。
    local payload
    local resp
    payload="$(/usr/bin/python3 - "$DEVICE" "$1" "$2" "$3" "$4" "${5:-}" <<'PY'
import json, sys

device_id, text, emotion, status, speak, action = sys.argv[1:7]
body = {
    "device_id": device_id,
    "text": text,
    "emotion": emotion,
    "status": status,
    "speak": speak == "true",
}
if action:
    body["action"] = action
print(json.dumps(body, ensure_ascii=False))
PY
)"
    resp="$(curl -s --max-time 30 -X POST "$SERVER/xiaozhi/event/push" \
        -H "Content-Type: application/json" \
        -d "$payload")"
    printf '%s\n' "$resp"
    if ! printf '%s' "$resp" | grep -q '"ok":true'; then
        echo "✗ 推送失败：$resp" >&2
    fi
}

require_device() {
    # 判"目标设备 id 在不在 devices 列表里"，不是判"在线总数==1"：
    # mock 设备与真机同时在线（工具自己支持的场景）不该被这里误判拒绝。
    if ! curl -s --max-time 5 "$SERVER/xiaozhi/event/devices" | /usr/bin/python3 -c '
import json, sys
try:
    devices = json.load(sys.stdin).get("devices") or []
except Exception:
    devices = []
sys.exit(0 if sys.argv[1] in devices else 1)
' "$DEVICE"; then
        echo "✗ 机器人不在线（目标设备：$DEVICE），先等它回连再拍" >&2
        exit 1
    fi
}

case "${1:-}" in
    away)
        require_device
        # 声明状态走真实接口：来访应答要读它，才会说出"正在开会、几点回来"。
        # expected_return 必须发完整 ISO 时间——服务端 _parse_iso 靠
        # datetime.fromisoformat 校验，光传 "11:30" 判非法会吃 400
        # （HH:MM 归一化只在语音路径有，这里走的是 HTTP 直发）。
        EXPECTED_RETURN="$(/usr/bin/python3 -c '
import datetime
now = datetime.datetime.now()
target = now.replace(hour=11, minute=30, second=0, microsecond=0)
if target <= now:
    target += datetime.timedelta(days=1)
print(target.isoformat(timespec="seconds"))
')"
        STATUS_RESP="$(curl -s --max-time 10 -X POST "$SERVER/xiaozhi/status" \
            -H "Content-Type: application/json" \
            -d "{\"state\":\"meeting\",\"expected_return\":\"$EXPECTED_RETURN\"}")"
        if ! printf '%s' "$STATUS_RESP" | grep -q '"ok":true'; then
            echo "✗ 声明会议中失败：$STATUS_RESP" >&2
            exit 1
        fi
        push "$TEXT_ACK" happy 在岗 true nod
        echo "→ 已声明会议中（预计 11:30 回来，服务端记录 $EXPECTED_RETURN）。等这句播完再走开，然后跑 sleep"
        ;;
    sleep)
        require_device
        push "$TEXT_SLEEP" sleepy 休眠 false
        echo "→ 小飞已进入休眠画面（静音）"
        ;;
    visitor)
        require_device
        push "$TEXT_VISITOR" neutral 有人来访 true center
        ;;
    recorded)
        require_device
        push "$TEXT_RECORDED" happy 留言 true nod
        ;;
    back)
        require_device
        push "${2:-$TEXT_BACK}" happy 在岗 true nod
        ;;
    reset)
        RESET_STATUS_RESP="$(curl -s --max-time 10 -X POST "$SERVER/xiaozhi/status" \
            -H "Content-Type: application/json" -d '{"state":"available"}')"
        if ! printf '%s' "$RESET_STATUS_RESP" | grep -q '"ok":true'; then
            echo "✗ 状态回在岗失败：$RESET_STATUS_RESP" >&2
        fi
        /usr/bin/python3 - "$LEDGER" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if path.exists():
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pending"] = []
    data["away_started_at"] = None
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("→ 离席台账已清空")
else:
    print("→ 台账文件不存在，跳过")
PY
        push "待机" neutral 待机 false center
        echo "→ 主人状态回在岗，画面回待机"
        ;;
    *)
        sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
