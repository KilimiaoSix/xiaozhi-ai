"""线上告警的 HTTP 入口（需求文档流程七 + 桌面端告警管理列表）。

五个接口：

- POST /xiaozhi/incident/webhook            监控系统 / 演示脚本把告警打进来
- GET  /xiaozhi/incident/latest             当前最该关注的故障 + 今天的故障列表
- GET  /xiaozhi/incident/list               两条告警链路的归一化合并列表（桌面端）
- POST /xiaozhi/incident/{id}/ack           桌面端标记已处理
- POST /xiaozhi/incident/{id}/diagnose      桌面端对指定故障触发只读诊断

这一层只做鉴权、参数校验与序列化；要不要播报、怎么降噪、什么时候宣布恢复
全在 IncidentManager 里。校验失败（severity 写错、缺 service）一律 400 并把
原因带回去——监控接进来的当天，这条错误信息就是唯一的排障线索。

list 接口额外合并 alert_relay（SAE 值班中继）的告警：**只读**它的
recent()/get()，绝不触碰其状态机。两套链路的字段形状完全不同，这里归一成
桌面端约定的一份契约（见 _normalize_* 两个函数）；中继条目的 ack/diagnose
都回 400——它的处理闭环在飞书侧（认领后自动诊断），桌面端只看不动。
"""

import json
from datetime import datetime
from typing import Any, Optional

from aiohttp import web

from core.api.base_handler import BaseHandler
from core.incident_manager import (
    OUTCOME_ACCEPTED,
    OUTCOME_ACKED,
    OUTCOME_ALREADY_ACKED,
    OUTCOME_ALREADY_RECOVERED,
    OUTCOME_ALREADY_RUNNING,
    OUTCOME_NOT_FOUND,
    get_incident_manager,
)

TAG = __name__

LIST_STATES = ("firing", "observing", "recovered", "all")

# 中继告警级别 → 本仓库统一严重度。中继没有 P3 一档，未知级别按 P2 兜底：
# 宁可把警告排高一档，也不能让真告警沉到列表底部。
_RELAY_LEVEL_TO_SEVERITY = {"紧急": "P0", "严重": "P1", "警告": "P2"}
# 终态 = 中继闭环已收尾（诊断完 / 有人自己看 / 失败收场），列表语义归为 recovered
_RELAY_TERMINAL_STATES = frozenset({"DIAGNOSED", "DECLINED", "FAILED"})
# 有人认领过（含拒绝）就视为 acknowledged：值班中继里「认领」就是「我来处理」
_RELAY_HANDLED_STATES = frozenset({"CLAIMED", "DIAGNOSING", "DIAGNOSED", "DECLINED"})

RELAY_OP_REJECTED = (
    "该条目来自值班中继（alert_relay），处理闭环在飞书侧完成（认领后自动诊断），"
    "桌面端仅展示，不支持此操作"
)


def _epoch_to_iso(value: Any) -> Optional[str]:
    """中继用 epoch 秒记时，桌面端契约统一 ISO 字符串（与 incident 链路一致）。"""
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def _normalize_incident_snapshot(snapshot: dict, running: bool) -> dict:
    """incident_manager 的 snapshot / 落盘 JSON → 桌面端统一契约。

    diagnosis 的四态：任务在跑 = running（此时忽略上一轮结论，桌面端要看到
    的是「正在跑」）；有结论按 ok 分 done / failed；从没诊断过 = null。
    """
    if running:
        diagnosis = {"state": "running", "summary": "", "finished_at": None}
    else:
        raw = snapshot.get("diagnosis")
        if not isinstance(raw, dict) or not raw:
            diagnosis = None
        elif raw.get("ok"):
            diagnosis = {
                "state": "done",
                "summary": str(raw.get("summary") or ""),
                "finished_at": raw.get("at"),
            }
        else:
            diagnosis = {
                "state": "failed",
                "summary": str(raw.get("error") or raw.get("summary") or "诊断失败"),
                "finished_at": raw.get("at"),
            }
    return {
        "id": str(snapshot.get("incident_id") or ""),
        "source": "incident",
        "service": str(snapshot.get("service") or ""),
        "severity": str(snapshot.get("severity") or "P3"),
        "title": str(snapshot.get("title") or ""),
        "message": str(snapshot.get("message") or ""),
        "state": str(snapshot.get("state") or "firing"),
        "repeat_count": int(snapshot.get("repeat_count") or 1),
        "first_seen_at": snapshot.get("first_seen_at"),
        "last_seen_at": snapshot.get("last_seen_at"),
        "recovered_at": snapshot.get("recovered_at"),
        "announced": bool(snapshot.get("announced")),
        # 老落盘文件没有 acknowledged 键，缺省 False
        "acknowledged": bool(snapshot.get("acknowledged")),
        "simulated": bool(snapshot.get("simulated")),
        "diagnosis": diagnosis,
        "timeline": [
            entry for entry in (snapshot.get("timeline") or []) if isinstance(entry, dict)
        ],
    }


def _normalize_relay_record(record: dict) -> Optional[dict]:
    """alert_relay 的 RelayRecord.to_dict() → 桌面端统一契约。

    中继没有恢复观察的概念：终态（诊断完/拒绝/失败）归 recovered，
    其余一律 firing。last_seen 用 max(created, updated) 兜底——to_dict()
    不导出 last_seen_at，而 updated_at 只在状态流转时变，这是只读约束下
    能拿到的最接近值。
    """
    if not isinstance(record, dict):
        return None
    alert_id = str(record.get("alert_id") or "").strip()
    if not alert_id:
        return None
    alert = record.get("alert") if isinstance(record.get("alert"), dict) else {}

    created = record.get("created_at")
    updated = record.get("updated_at") or created
    try:
        last_seen_epoch = max(float(created or 0), float(updated or 0))
    except (TypeError, ValueError):
        last_seen_epoch = 0
    state = str(record.get("state") or "")
    terminal = state in _RELAY_TERMINAL_STATES

    if state == "DIAGNOSING":
        diagnosis = {"state": "running", "summary": "", "finished_at": None}
    elif state == "DIAGNOSED":
        raw = record.get("diagnosis") if isinstance(record.get("diagnosis"), dict) else {}
        summary = str(raw.get("root_cause") or raw.get("title") or "").strip()
        diagnosis = {
            "state": "done",
            "summary": summary,
            "finished_at": _epoch_to_iso(updated),
        }
    elif state == "FAILED":
        diagnosis = {
            "state": "failed",
            "summary": str(record.get("error") or "诊断失败"),
            "finished_at": _epoch_to_iso(updated),
        }
    else:
        diagnosis = None

    service = str(
        alert.get("workload") or alert.get("target") or alert.get("cluster") or ""
    ).strip()
    message_parts = [
        str(alert.get(key) or "").strip() for key in ("cluster", "namespace", "rule")
    ]
    return {
        "id": alert_id,
        "source": "alert_relay",
        "service": service or "未知服务",
        "severity": _RELAY_LEVEL_TO_SEVERITY.get(
            str(alert.get("level") or "").strip(), "P2"
        ),
        "title": str(alert.get("summary") or "").strip() or service or alert_id,
        "message": " / ".join(part for part in message_parts if part),
        "state": "recovered" if terminal else "firing",
        # 中继 repeat_count 记的是「重复了几次」，+1 归一成「一共出现几次」
        "repeat_count": int(record.get("repeat_count") or 0) + 1,
        "first_seen_at": _epoch_to_iso(created),
        "last_seen_at": _epoch_to_iso(last_seen_epoch),
        "recovered_at": _epoch_to_iso(updated) if terminal else None,
        "announced": bool(record.get("robot_delivered")),
        "acknowledged": bool(record.get("claimed_by")) or state in _RELAY_HANDLED_STATES,
        # 中继链路没有模拟位；真要演示走 incident webhook 的 simulated
        "simulated": False,
        "diagnosis": diagnosis,
        "timeline": [
            {
                "at": _epoch_to_iso(entry.get("at")),
                "event": str(entry.get("state") or ""),
                "detail": str(entry.get("note") or ""),
            }
            for entry in (record.get("history") or [])
            if isinstance(entry, dict)
        ],
    }


class IncidentHandler(BaseHandler):
    def __init__(self, config: dict, device_registry=None, manager=None):
        super().__init__(config)
        self.device_registry = device_registry
        self.manager = manager or get_incident_manager(config)
        # HTTP 侧通常最早装配起来，顺手把 config 与注册表接上，
        # 语音路径就不必等设备连上来才有得用（同 PomodoroHandler）。
        self.manager.bind(
            config=config, device_registry=device_registry, logger=self.logger
        )
        # alert_relay 在 http_server 里晚于本 handler 装配，由 set_alert_relay 注入；
        # 没注入（presence_server / 部分测试）时列表自动退化为仅 incident 链路
        self._alert_relay = None
        auth_config = config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        self.auth_key = config["server"].get("auth_key", "")

    def set_alert_relay(self, service) -> None:
        """注入值班中继实例。本 handler 只读它的 recent()/get()，不碰状态机。"""
        self._alert_relay = service

    def _authorized(self, request) -> bool:
        if not self.auth_enable:
            return True
        token = request.headers.get("authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        return bool(self.auth_key) and token == self.auth_key

    def _json_response(self, payload: dict, status: int = 200):
        response = web.Response(
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            content_type="application/json",
            status=status,
        )
        self._add_cors_headers(response)
        return response

    def _unauthorized(self):
        return self._json_response({"success": False, "message": "unauthorized"}, 401)

    async def handle_webhook(self, request):
        """Body: {"service","severity":"P0|P1|P2|P3","title", ...}，详见 IncidentManager。"""
        if not self._authorized(request):
            return self._unauthorized()

        try:
            data = await request.json()
        except Exception:
            return self._json_response(
                {"success": False, "message": "invalid json body"}, 400
            )
        if not isinstance(data, dict):
            return self._json_response(
                {"success": False, "message": "body must be a json object"}, 400
            )

        try:
            result = await self.manager.handle_webhook(data)
        except ValueError as e:
            # 字段非法：监控侧的问题，把原因原样告诉它
            return self._json_response({"success": False, "message": str(e)}, 400)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理告警 webhook 失败: {e}")
            return self._json_response(
                {"success": False, "message": f"webhook failed: {e}"}, 502
            )

        self.logger.bind(tag=TAG).info(
            f"告警 {result['incident_id']} 处理结果: {result['outcome']}"
            f"（播报={result['announced']}）"
        )
        return self._json_response({"success": True, **result})

    async def handle_latest(self, request):
        """当前活跃故障 + 今日故障列表（日终总结与桌面端用）。"""
        if not self._authorized(request):
            return self._unauthorized()

        try:
            active = self.manager.active_incident()
            today = self.manager.list_today()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"读取故障列表失败: {e}")
            return self._json_response(
                {"success": False, "message": f"read failed: {e}"}, 502
            )

        return self._json_response(
            {"success": True, "active": active, "count": len(today), "today": today}
        )

    # ------------------------------------------------------------ 桌面端列表

    async def handle_list(self, request):
        """归一化合并列表：incident 链路（内存 + 落盘）∪ alert_relay。

        查询参数：date（YYYY-MM-DD，缺省今天）、state（firing|observing|
        recovered|all，缺省 all）、limit（正整数，缺省 50）。参数非法一律 400
        ——桌面端是唯一调用方，宽松放行只会把它的 bug 埋起来。
        """
        if not self._authorized(request):
            return self._unauthorized()

        day = str(request.query.get("date") or "").strip()
        if day:
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return self._json_response(
                    {"success": False, "message": "date 必须是 YYYY-MM-DD"}, 400
                )
        else:
            day = self.manager.current_day()

        state = str(request.query.get("state") or "all").strip().lower()
        if state not in LIST_STATES:
            return self._json_response(
                {
                    "success": False,
                    "message": "state 必须是 firing/observing/recovered/all",
                },
                400,
            )

        raw_limit = str(request.query.get("limit") or "50").strip()
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 0
        if limit <= 0:
            return self._json_response(
                {"success": False, "message": "limit 必须是正整数"}, 400
            )

        try:
            rows = [
                _normalize_incident_snapshot(
                    snapshot, self.manager.diagnosis_running(snapshot.get("incident_id"))
                )
                for snapshot in self.manager.list_for_date(day)
            ]
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"读取故障列表失败: {e}")
            return self._json_response(
                {"success": False, "message": f"read failed: {e}"}, 502
            )

        rows.extend(self._relay_rows(day))

        if state != "all":
            rows = [row for row in rows if row["state"] == state]
        # 最近有动静的排前面；桌面端还会按严重度/状态再排，这里只保证稳定基序
        rows.sort(key=lambda row: str(row.get("last_seen_at") or ""), reverse=True)
        rows = rows[:limit]

        return self._json_response(
            {
                "success": True,
                "date": day,
                "state": state,
                "count": len(rows),
                "incidents": rows,
            }
        )

    def _relay_rows(self, day: str) -> list:
        """读值班中继的告警并归一化。中继挂了只记日志——列表主体是 incident
        链路，不能因为旁路取数失败整个 502。"""
        if self._alert_relay is None:
            return []
        try:
            records = self._alert_relay.recent(limit=200)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"读取值班中继告警失败，本次仅展示 incident 链路: {e}")
            return []
        rows = []
        for record in records or []:
            row = _normalize_relay_record(record)
            if row is not None and str(row.get("first_seen_at") or "")[:10] == day:
                rows.append(row)
        return rows

    def _relay_has(self, incident_id: str) -> bool:
        if self._alert_relay is None:
            return False
        try:
            return self._alert_relay.get(incident_id) is not None
        except Exception:
            return False

    # ------------------------------------------------------------ 桌面端操作

    async def handle_ack(self, request):
        """标记已处理。已恢复 409（时间线已定稿）、未知 404、中继条目 400。"""
        if not self._authorized(request):
            return self._unauthorized()
        incident_id = str(request.match_info.get("incident_id") or "").strip()

        try:
            result = self.manager.ack(incident_id)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"标记已处理失败: {e}")
            return self._json_response(
                {"success": False, "message": f"ack failed: {e}"}, 502
            )

        outcome = result["outcome"]
        if outcome in (OUTCOME_ACKED, OUTCOME_ALREADY_ACKED):
            self.logger.bind(tag=TAG).info(f"故障 {incident_id} 已标记处理（{outcome}）")
            return self._json_response(
                {"success": True, "incident_id": incident_id, "acknowledged": True}
            )
        if outcome == OUTCOME_ALREADY_RECOVERED:
            return self._json_response(
                {"success": False, "message": "故障已恢复，时间线已定稿，无需标记"}, 409
            )
        if self._relay_has(incident_id):
            return self._json_response(
                {"success": False, "message": RELAY_OP_REJECTED}, 400
            )
        return self._json_response(
            {"success": False, "message": f"没有故障 {incident_id}"}, 404
        )

    async def handle_diagnose(self, request):
        """对指定故障触发只读诊断，立即返回，结果异步落时间线并播报。

        同一故障已有诊断在跑 → 409 + 当前诊断状态（桌面端把它归一成
        「诊断中」而不是报错）；中继条目 400（它有自有诊断闭环）。
        """
        if not self._authorized(request):
            return self._unauthorized()
        incident_id = str(request.match_info.get("incident_id") or "").strip()

        try:
            result = await self.manager.request_diagnosis(incident_id)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"触发诊断失败: {e}")
            return self._json_response(
                {"success": False, "message": f"diagnose failed: {e}"}, 502
            )

        outcome = result["outcome"]
        if outcome == OUTCOME_ACCEPTED:
            self.logger.bind(tag=TAG).info(f"故障 {incident_id} 诊断已受理")
            return self._json_response(
                {
                    "success": True,
                    "accepted": True,
                    "incident_id": incident_id,
                    "diagnosis": {"state": "running"},
                }
            )
        if outcome == OUTCOME_ALREADY_RUNNING:
            return self._json_response(
                {
                    "success": False,
                    "message": "这个故障的诊断还在跑",
                    "diagnosis": {"state": "running"},
                },
                409,
            )
        if self._relay_has(incident_id):
            return self._json_response(
                {"success": False, "message": RELAY_OP_REJECTED}, 400
            )
        return self._json_response(
            {"success": False, "message": f"没有故障 {incident_id}"}, 404
        )
