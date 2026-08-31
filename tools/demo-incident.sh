#!/usr/bin/env bash
# 流程七「线上故障与诊断协助」的拍摄用触发器。
#
# 走的是真链路：告警进 /xiaozhi/incident/webhook，诊断起一个真的 `claude -p`
# 子进程去看日志与配置，恢复要安静走完观察窗（data/.config.yaml 里 45 秒）
# 才播报。只有告警内容是模拟的——simulated 恒为 true，播报带「模拟」前缀，
# 现场不会有人误当成真故障。
#
# 用法（分镜顺序）：
#   demo-incident.sh alert [ID]      # 7-1/7-2 P1 告警，小飞打断当前状态开口播报
#   demo-incident.sh diagnose [ID]   # 7-3 起诊断 Agent（也可以让工程师语音说"启动诊断"）
#   demo-incident.sh resolve [ID]    # 7-4 指标恢复，进 45 秒观察窗，安静走完才播恢复
#   demo-incident.sh status [ID]     # 查这条故障现在什么状态
#   demo-incident.sh reset           # 换一条干净的故障 ID 重拍
#
# ID 不传时用 take-1。**每次重拍都要换 ID**，同 service+title 的重复上报会被
# 归并成同一条故障，不会二次播报。

set -uo pipefail

SERVER="${DESKPET_SERVER:-http://127.0.0.1:8003}"
DEVICE="${DESKPET_DEVICE_ID:-dc:da:0c:26:9a:60}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
XZ_DIR="${XZ_DIR:-$REPO_ROOT/server/main/xiaozhi-server}"
PY="$XZ_DIR/.venv/bin/python"
ID="${2:-take-1}"

TITLE="接口错误率升高"
MESSAGE="支付回调接口 5 分钟内错误率 12%，超过 5% 阈值，影响下单支付；二十分钟前刚上线过一次配置变更"

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
        echo "✗ 机器人不在线（目标设备：$DEVICE）" >&2
        exit 1
    fi
}

case "${1:-}" in
    alert)
        require_device
        cd "$XZ_DIR" || exit 1
        "$PY" scripts/simulate_incident.py --server "$SERVER" --severity P1 --incident-id "$ID" \
            --title "$TITLE" --message "$MESSAGE" 2>&1 \
            | grep -E '"outcome"|"announced"|HTTP|请求失败'
        echo "→ 故障 ID: $ID"
        echo "  ⚠️ 触发这一刻别说话：麦克风拾到人声时推送只等 3 秒就降级成"只显示不出声""
        ;;
    diagnose)
        require_device
        curl -s --max-time 20 -X POST "$SERVER/xiaozhi/incident/$ID/diagnose" \
            -H "Content-Type: application/json" -d '{}'
        printf '\n→ 诊断已受理，claude -p 子进程在跑，结论出来会自动播报（几十秒）\n'
        ;;
    resolve)
        require_device
        cd "$XZ_DIR" || exit 1
        "$PY" scripts/simulate_incident.py --server "$SERVER" --resolve --incident-id "$ID" \
            --title "$TITLE" 2>&1 | grep -E '"outcome"|HTTP|请求失败'
        echo "→ 进入 45 秒观察窗。这 45 秒里不能再来告警，安静走完才播恢复"
        ;;
    status)
        curl -s --max-time 5 "$SERVER/xiaozhi/incident/latest" \
            | /usr/bin/python3 -c '
import json, sys
body = json.load(sys.stdin)
if not body.get("success"):
    print("✗", body.get("message") or "查询失败")
    raise SystemExit(1)
d = body.get("active") or {}
if not d:
    print("(没有故障记录)"); raise SystemExit
print("ID      :", d.get("incident_id"))
print("状态    :", d.get("state"), "| 观察中:", d.get("observing"), "| 已恢复:", d.get("recovered"))
print("已播报  :", d.get("announced"), "| 诊断:", (d.get("diagnosis") or {}).get("summary") or "(无)")
for ev in d.get("timeline") or []:
    print("  ", ev.get("at"), ev.get("event"), "-", (ev.get("detail") or "")[:60])
'
        ;;
    reset)
        echo "→ 不用清任何东西，换个没用过的 ID 就是一条全新的故障："
        echo "     demo-incident.sh alert take-2"
        echo "     demo-incident.sh diagnose take-2"
        echo "     demo-incident.sh resolve take-2"
        ;;
    *)
        sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
