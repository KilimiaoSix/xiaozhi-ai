"""告警管理列表接口测试（list / ack / diagnose，桌面端契约）。

走 aiohttp 测试客户端，manager 用真实实现但注入假推送 / 假时钟 / 闸门式假 sleep
与临时落盘目录；诊断一律注入假执行器或「真 DiagnosisRunner + 假子进程」
（同 tests/test_incident_diagnosis.py 的注入方式），绝不真跑 claude。
alert_relay 只读：用最小替身提供 recent()/get()，字段形状对齐
core/alert_relay/models.RelayRecord.to_dict()。
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from aiohttp import web

import config.settings
from config.config_loader import get_project_dir, read_config
from config.logger import setup_logging
from core.utils.cache.manager import CacheType, cache_manager

# BaseHandler 构造时会 setup_logging()，配置缓存是冷的就会走 asyncio.run(load_config())，
# 而 handler 是在测试的事件循环里构造的。趁导入阶段先把缓存捂热
# （同 tests/test_incident_handler.py）。
_repo_config = read_config(get_project_dir() + "config.yaml")
cache_manager.set(CacheType.CONFIG, "main_config", _repo_config)
config.settings.config_file_valid = True
setup_logging(_repo_config)

from core.api.incident_handler import IncidentHandler  # noqa: E402
from core.incident_diagnosis import DiagnosisRunner  # noqa: E402
from core.incident_manager import IncidentManager  # noqa: E402
from core.incident_routes import add_incident_routes  # noqa: E402


CONN = object()
NOW = datetime(2026, 8, 19, 10, 0, 0)
TODAY = "2026-08-19"
YESTERDAY = "2026-08-18"


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class GatedSleep:
    """恢复观察窗的假 sleep：不放行就停在窗口里，测试能精确控制恢复时机。"""

    def __init__(self) -> None:
        self.calls = []
        self.gate = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await self.gate.wait()

    def release(self) -> None:
        self.gate.set()


class PushRecorder:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, conn, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return True

    @property
    def texts(self):
        return [call["text"] for call in self.calls]


class GatedRunner:
    """假诊断执行器：停在闸门上，覆盖「诊断进行中」的并发窗口。"""

    def __init__(self, result=None) -> None:
        self.result = result or {"ok": True, "summary": "复盘结论：上游超时", "error": ""}
        self.gate = asyncio.Event()
        self.incidents = []

    async def run(self, incident, on_result=None):
        self.incidents.append(incident)
        await self.gate.wait()
        if on_result is not None:
            await on_result(incident.get("incident_id"), self.result)
        return self.result


# ---------------------------------------------------------------- 假子进程
# 与 tests/test_incident_diagnosis.py 相同的注入方式：真 DiagnosisRunner，假 spawn。


def cli_json(result_text: str) -> bytes:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result_text,
        },
        ensure_ascii=False,
    ).encode("utf-8")


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def fake_subprocess_runner(result_text: str) -> DiagnosisRunner:
    async def spawn(argv, cwd, env):
        return FakeProcess(cli_json(result_text))

    return DiagnosisRunner({"incident": {"timeout_s": 5}}, spawn=spawn)


# ---------------------------------------------------------------- alert_relay 替身


def epoch(dt: datetime) -> float:
    return dt.timestamp()


def relay_record(**overrides) -> dict:
    """对齐 RelayRecord.to_dict() 的字段形状（只挑列表归一化会读的部分也全量给出）。"""
    record = {
        "alert_id": "abcd1234ef567890-1755568800",
        "state": "AWAITING_REPLY",
        "created_at": epoch(NOW),
        "updated_at": epoch(NOW),
        "repeat_count": 2,
        "alert": {
            "level": "紧急",
            "cluster": "hu",
            "namespace": "prod",
            "target": "igc-server-7f8b9c6d5-abcde",
            "workload": "igc-server",
            "keyword": "OOMKilled",
            "alert_time": "2026-08-19 10:00:00",
            "policy_url": "",
            "project_id": "",
            "cluster_id": "",
            "rule": "容器内存告警",
            "summary": "igc-server OOMKilled",
            "fingerprint": "abcd1234ef567890",
        },
        "robot_delivered": True,
        "robot_error": "",
        "feishu_message_id": "om_x",
        "feishu_chat_id": "oc_x",
        "feishu_error": "",
        "claimed_by": "",
        "reply_text": "",
        "diagnosis": None,
        "error": "",
        "warnings": [],
        "history": [
            {"state": "NOTIFIED", "at": epoch(NOW), "note": ""},
            {"state": "AWAITING_REPLY", "at": epoch(NOW), "note": ""},
        ],
    }
    record.update(overrides)
    return record


class FakeAlertRelay:
    def __init__(self, records=None, *, explode=False):
        self.records = list(records or [])
        self.explode = explode

    def recent(self, limit=20):
        if self.explode:
            raise RuntimeError("中继取数失败")
        return list(self.records[: max(1, int(limit))])

    def get(self, alert_id):
        for record in self.records:
            if record["alert_id"] == alert_id:
                return dict(record)
        return None


# ---------------------------------------------------------------- 装配


def build_app(
    tmp_path,
    *,
    auth=False,
    alert_relay=None,
    runner=None,
    observe_seconds=300,
):
    config = {
        "server": {
            "auth": {"enabled": auth},
            "auth_key": "test-secret" if auth else "",
        },
        "incident": {"dedup_cooldown_s": 120, "observe_seconds": observe_seconds},
    }
    push = PushRecorder()
    clock = FakeClock()
    sleep = GatedSleep()
    manager = IncidentManager(
        config,
        push_event=push,
        device_resolver=lambda: CONN,
        storage_dir=tmp_path,
        clock=clock,
        sleep=sleep,
        diagnosis_runner=runner,
    )
    handler = IncidentHandler(config, manager=manager)
    if alert_relay is not None:
        handler.set_alert_relay(alert_relay)
    app = web.Application()
    add_incident_routes(app, handler)
    app["manager"] = manager
    app["push"] = push
    app["sleep"] = sleep
    app["clock"] = clock
    return app


async def make_client(aiohttp_client, tmp_path, **kwargs):
    app = build_app(tmp_path, **kwargs)
    return await aiohttp_client(app), app


def firing(**overrides):
    payload = {
        "service": "demo-api",
        "severity": "P1",
        "title": "接口错误率升高",
        "message": "支付回调错误率 12%",
        "simulated": True,
    }
    payload.update(overrides)
    return payload


def write_disk_record(tmp_path, day: str, incident_id: str, **overrides) -> dict:
    """直接落一份历史时间线文件，模拟「上一次进程留下的记录」。"""
    record = {
        "incident_id": incident_id,
        "service": "old-api",
        "severity": "P2",
        "title": "历史告警",
        "message": "",
        "metric": "",
        "value": None,
        "source": "",
        "simulated": False,
        "started_at": f"{day}T08:00:00",
        "first_seen_at": f"{day}T08:00:00",
        "last_seen_at": f"{day}T08:30:00",
        "state": "recovered",
        "repeat_count": 3,
        "announced": False,
        "last_announced_at": None,
        "last_notified_at": f"{day}T08:00:00",
        "resolved_at": f"{day}T08:20:00",
        "recovered_at": f"{day}T08:30:00",
        "observe_seconds": 300,
        "diagnosis": None,
        "timeline": [{"at": f"{day}T08:00:00", "event": "received", "detail": "首次收到告警"}],
        "observing": False,
        "recovered": True,
    }
    record.update(overrides)
    path = tmp_path / f"{day}-{incident_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


EXPECTED_KEYS = {
    "id",
    "source",
    "service",
    "severity",
    "title",
    "message",
    "state",
    "repeat_count",
    "first_seen_at",
    "last_seen_at",
    "recovered_at",
    "announced",
    "acknowledged",
    "simulated",
    "diagnosis",
    "timeline",
}


# ================================================================ list


@pytest.mark.asyncio
async def test_list_empty_returns_success(aiohttp_client, tmp_path):
    client, _ = await make_client(aiohttp_client, tmp_path)

    response = await client.get("/xiaozhi/incident/list")

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    assert body["date"] == TODAY
    assert body["incidents"] == []
    assert body["count"] == 0


@pytest.mark.asyncio
async def test_list_merges_incident_and_alert_relay_sources(aiohttp_client, tmp_path):
    relay = FakeAlertRelay([relay_record()])
    client, app = await make_client(aiohttp_client, tmp_path, alert_relay=relay)
    await client.post("/xiaozhi/incident/webhook", json=firing())
    app["clock"].advance(300)
    await client.post("/xiaozhi/incident/webhook", json=firing())  # 冷却外重复，刷新 last_seen

    body = await (await client.get("/xiaozhi/incident/list")).json()

    assert body["count"] == 2
    by_source = {row["source"]: row for row in body["incidents"]}
    incident = by_source["incident"]
    relay_row = by_source["alert_relay"]

    assert set(incident.keys()) == EXPECTED_KEYS
    assert set(relay_row.keys()) == EXPECTED_KEYS

    assert incident["service"] == "demo-api"
    assert incident["severity"] == "P1"
    assert incident["state"] == "firing"
    assert incident["simulated"] is True
    assert incident["acknowledged"] is False
    assert incident["diagnosis"] is None

    assert relay_row["id"] == "abcd1234ef567890-1755568800"
    assert relay_row["severity"] == "P0"  # 紧急 → P0
    assert relay_row["state"] == "firing"  # AWAITING_REPLY 未终结
    assert relay_row["service"] == "igc-server"
    assert relay_row["title"] == "igc-server OOMKilled"
    assert relay_row["repeat_count"] == 3  # 中继 repeat_count 记的是重复次数，+1 归一成出现次数
    assert relay_row["first_seen_at"] == f"{TODAY}T10:00:00"
    assert relay_row["simulated"] is False
    assert relay_row["diagnosis"] is None
    assert relay_row["timeline"][0]["event"] == "NOTIFIED"

    # 归并列表按 last_seen_at 倒序：incident 在 10:05 又报了一次，应排在前面
    assert body["incidents"][0]["source"] == "incident"


@pytest.mark.asyncio
async def test_list_relay_terminal_states_and_diagnosis_mapping(aiohttp_client, tmp_path):
    relay = FakeAlertRelay(
        [
            relay_record(
                alert_id="diagnosed-1",
                state="DIAGNOSED",
                claimed_by="ou_x",
                diagnosis={"title": "OOM", "root_cause": "内存泄漏导致容器反复重启", "suggestion": []},
                updated_at=epoch(NOW) + 600,
            ),
            relay_record(alert_id="failed-1", state="FAILED", error="诊断超时", updated_at=epoch(NOW) + 60),
            relay_record(alert_id="running-1", state="DIAGNOSING", claimed_by="ou_x"),
        ]
    )
    client, _ = await make_client(aiohttp_client, tmp_path, alert_relay=relay)

    body = await (await client.get("/xiaozhi/incident/list")).json()
    rows = {row["id"]: row for row in body["incidents"]}

    assert rows["diagnosed-1"]["state"] == "recovered"
    assert rows["diagnosed-1"]["acknowledged"] is True
    assert rows["diagnosed-1"]["diagnosis"]["state"] == "done"
    assert "内存泄漏" in rows["diagnosed-1"]["diagnosis"]["summary"]
    assert rows["diagnosed-1"]["recovered_at"] == f"{TODAY}T10:10:00"

    assert rows["failed-1"]["state"] == "recovered"
    assert rows["failed-1"]["diagnosis"]["state"] == "failed"
    assert rows["failed-1"]["diagnosis"]["summary"] == "诊断超时"

    assert rows["running-1"]["state"] == "firing"
    assert rows["running-1"]["diagnosis"]["state"] == "running"


@pytest.mark.asyncio
async def test_list_includes_disk_history_from_prior_process(aiohttp_client, tmp_path):
    write_disk_record(tmp_path, TODAY, "old-incident-1")
    client, _ = await make_client(aiohttp_client, tmp_path)

    body = await (await client.get("/xiaozhi/incident/list")).json()

    assert body["count"] == 1
    row = body["incidents"][0]
    assert row["id"] == "old-incident-1"
    assert row["state"] == "recovered"
    assert row["acknowledged"] is False  # 老文件没有该字段，归一化补 False


@pytest.mark.asyncio
async def test_list_state_filter(aiohttp_client, tmp_path):
    write_disk_record(tmp_path, TODAY, "recovered-1")
    client, _ = await make_client(aiohttp_client, tmp_path)
    await client.post("/xiaozhi/incident/webhook", json=firing())

    firing_only = await (await client.get("/xiaozhi/incident/list?state=firing")).json()
    recovered_only = await (
        await client.get("/xiaozhi/incident/list?state=recovered")
    ).json()
    everything = await (await client.get("/xiaozhi/incident/list?state=all")).json()

    assert [row["state"] for row in firing_only["incidents"]] == ["firing"]
    assert [row["state"] for row in recovered_only["incidents"]] == ["recovered"]
    assert everything["count"] == 2


@pytest.mark.asyncio
async def test_list_date_param_selects_other_day(aiohttp_client, tmp_path):
    write_disk_record(tmp_path, YESTERDAY, "yesterday-1")
    yesterday_relay = relay_record(
        alert_id="relay-yesterday",
        created_at=epoch(NOW - timedelta(days=1)),
        updated_at=epoch(NOW - timedelta(days=1)),
    )
    client, _ = await make_client(
        aiohttp_client, tmp_path, alert_relay=FakeAlertRelay([yesterday_relay])
    )
    await client.post("/xiaozhi/incident/webhook", json=firing())

    today_body = await (await client.get("/xiaozhi/incident/list")).json()
    yesterday_body = await (
        await client.get(f"/xiaozhi/incident/list?date={YESTERDAY}")
    ).json()

    # 今天只有 webhook 那一条，昨天的盘上记录与中继记录都不允许混进来
    assert today_body["count"] == 1
    assert today_body["incidents"][0]["source"] == "incident"
    assert yesterday_body["date"] == YESTERDAY
    assert {row["id"] for row in yesterday_body["incidents"]} == {
        "yesterday-1",
        "relay-yesterday",
    }


@pytest.mark.asyncio
async def test_list_limit_caps_results(aiohttp_client, tmp_path):
    client, _ = await make_client(aiohttp_client, tmp_path)
    for index in range(3):
        await client.post(
            "/xiaozhi/incident/webhook", json=firing(title=f"告警{index}")
        )

    body = await (await client.get("/xiaozhi/incident/list?limit=2")).json()

    assert body["count"] == 2
    assert len(body["incidents"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "date=2026-13-99",
        "date=不是日期",
        "state=weird",
        "limit=0",
        "limit=-1",
        "limit=abc",
    ],
)
async def test_list_rejects_invalid_params(aiohttp_client, tmp_path, query):
    client, _ = await make_client(aiohttp_client, tmp_path)

    response = await client.get(f"/xiaozhi/incident/list?{query}")

    assert response.status == 400
    body = await response.json()
    assert body["success"] is False
    assert body["message"]


@pytest.mark.asyncio
async def test_list_survives_alert_relay_failure(aiohttp_client, tmp_path):
    client, _ = await make_client(
        aiohttp_client, tmp_path, alert_relay=FakeAlertRelay(explode=True)
    )
    await client.post("/xiaozhi/incident/webhook", json=firing())

    response = await client.get("/xiaozhi/incident/list")

    assert response.status == 200
    body = await response.json()
    assert body["count"] == 1
    assert body["incidents"][0]["source"] == "incident"


@pytest.mark.asyncio
async def test_list_shows_running_diagnosis_then_done(aiohttp_client, tmp_path):
    runner = GatedRunner()
    client, app = await make_client(aiohttp_client, tmp_path, runner=runner)
    result = await client.post("/xiaozhi/incident/webhook", json=firing())
    incident_id = (await result.json())["incident_id"]

    await client.post(f"/xiaozhi/incident/{incident_id}/diagnose")
    running_body = await (await client.get("/xiaozhi/incident/list")).json()

    runner.gate.set()
    await app["manager"].wait_idle()
    done_body = await (await client.get("/xiaozhi/incident/list")).json()

    assert running_body["incidents"][0]["diagnosis"]["state"] == "running"
    done = done_body["incidents"][0]["diagnosis"]
    assert done["state"] == "done"
    assert done["summary"] == "复盘结论：上游超时"
    assert done["finished_at"]


# ================================================================ ack


@pytest.mark.asyncio
async def test_ack_marks_incident_and_persists(aiohttp_client, tmp_path):
    client, _ = await make_client(aiohttp_client, tmp_path)
    result = await client.post("/xiaozhi/incident/webhook", json=firing())
    incident_id = (await result.json())["incident_id"]

    response = await client.post(f"/xiaozhi/incident/{incident_id}/ack")

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    assert body["acknowledged"] is True

    listed = await (await client.get("/xiaozhi/incident/list")).json()
    assert listed["incidents"][0]["acknowledged"] is True
    assert any(
        event["event"] == "acknowledged"
        for event in listed["incidents"][0]["timeline"]
    )

    on_disk = json.loads(
        (tmp_path / f"{TODAY}-{incident_id}.json").read_text(encoding="utf-8")
    )
    assert on_disk["acknowledged"] is True


@pytest.mark.asyncio
async def test_ack_is_idempotent(aiohttp_client, tmp_path):
    client, _ = await make_client(aiohttp_client, tmp_path)
    result = await client.post("/xiaozhi/incident/webhook", json=firing())
    incident_id = (await result.json())["incident_id"]

    first = await client.post(f"/xiaozhi/incident/{incident_id}/ack")
    second = await client.post(f"/xiaozhi/incident/{incident_id}/ack")

    assert first.status == 200
    assert second.status == 200


@pytest.mark.asyncio
async def test_ack_recovered_returns_409(aiohttp_client, tmp_path):
    client, app = await make_client(aiohttp_client, tmp_path)
    result = await client.post("/xiaozhi/incident/webhook", json=firing())
    incident_id = (await result.json())["incident_id"]
    await client.post("/xiaozhi/incident/webhook", json=firing(status="resolved"))
    app["sleep"].release()
    await app["manager"].wait_idle()

    response = await client.post(f"/xiaozhi/incident/{incident_id}/ack")

    assert response.status == 409
    assert (await response.json())["success"] is False


@pytest.mark.asyncio
async def test_ack_unknown_returns_404(aiohttp_client, tmp_path):
    client, _ = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/incident/nope-404/ack")

    assert response.status == 404
    assert (await response.json())["success"] is False


@pytest.mark.asyncio
async def test_ack_alert_relay_source_returns_400(aiohttp_client, tmp_path):
    relay = FakeAlertRelay([relay_record(alert_id="relay-1")])
    client, _ = await make_client(aiohttp_client, tmp_path, alert_relay=relay)

    response = await client.post("/xiaozhi/incident/relay-1/ack")

    assert response.status == 400
    body = await response.json()
    assert body["success"] is False
    assert "值班中继" in body["message"]


@pytest.mark.asyncio
async def test_ack_disk_only_record_updates_file(aiohttp_client, tmp_path):
    """进程重启后内存态丢失，落盘的历史条目也要能标记（状态未定稿的那种）。"""
    write_disk_record(
        tmp_path, TODAY, "disk-firing-1", state="firing", recovered_at=None, recovered=False
    )
    client, _ = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/incident/disk-firing-1/ack")

    assert response.status == 200
    on_disk = json.loads(
        (tmp_path / f"{TODAY}-disk-firing-1.json").read_text(encoding="utf-8")
    )
    assert on_disk["acknowledged"] is True
    assert on_disk["timeline"][-1]["event"] == "acknowledged"


# ================================================================ diagnose


@pytest.mark.asyncio
async def test_diagnose_accepts_and_runs_fake_subprocess(aiohttp_client, tmp_path):
    """端到端走真 DiagnosisRunner + 假子进程：结果落时间线并照旧播报。"""
    client, app = await make_client(
        aiohttp_client, tmp_path, runner=fake_subprocess_runner("根因是磁盘满，建议清理日志。")
    )
    result = await client.post("/xiaozhi/incident/webhook", json=firing())
    incident_id = (await result.json())["incident_id"]

    response = await client.post(f"/xiaozhi/incident/{incident_id}/diagnose")

    assert response.status == 200
    body = await response.json()
    assert body["success"] is True
    assert body["accepted"] is True
    assert body["diagnosis"]["state"] == "running"

    await app["manager"].wait_idle()

    listed = await (await client.get("/xiaozhi/incident/list")).json()
    diagnosis = listed["incidents"][0]["diagnosis"]
    assert diagnosis["state"] == "done"
    assert diagnosis["summary"] == "根因是磁盘满，建议清理日志。"
    events = [event["event"] for event in listed["incidents"][0]["timeline"]]
    assert "diagnosis_started" in events
    assert "diagnosis_result" in events
    assert app["push"].texts[-1] == "诊断结果：根因是磁盘满，建议清理日志。"


@pytest.mark.asyncio
async def test_diagnose_concurrent_returns_409_without_second_process(
    aiohttp_client, tmp_path
):
    runner = GatedRunner()
    client, app = await make_client(aiohttp_client, tmp_path, runner=runner)
    result = await client.post("/xiaozhi/incident/webhook", json=firing())
    incident_id = (await result.json())["incident_id"]

    first = await client.post(f"/xiaozhi/incident/{incident_id}/diagnose")
    second = await client.post(f"/xiaozhi/incident/{incident_id}/diagnose")

    assert first.status == 200
    assert second.status == 409
    second_body = await second.json()
    assert second_body["success"] is False
    assert second_body["diagnosis"]["state"] == "running"

    runner.gate.set()
    await app["manager"].wait_idle()
    # 409 的那次绝不允许再起一个诊断进程
    assert len(runner.incidents) == 1


@pytest.mark.asyncio
async def test_diagnose_recovered_incident_for_review(aiohttp_client, tmp_path):
    """已恢复的故障允许事后诊断（复盘场景），结论照样落时间线。"""
    runner = GatedRunner()
    client, app = await make_client(aiohttp_client, tmp_path, runner=runner)
    result = await client.post("/xiaozhi/incident/webhook", json=firing())
    incident_id = (await result.json())["incident_id"]
    await client.post("/xiaozhi/incident/webhook", json=firing(status="resolved"))
    app["sleep"].release()
    await app["manager"].wait_idle()

    response = await client.post(f"/xiaozhi/incident/{incident_id}/diagnose")

    assert response.status == 200
    runner.gate.set()
    await app["manager"].wait_idle()

    listed = await (await client.get("/xiaozhi/incident/list")).json()
    row = listed["incidents"][0]
    assert row["state"] == "recovered"
    assert row["diagnosis"]["state"] == "done"


@pytest.mark.asyncio
async def test_diagnose_unknown_returns_404(aiohttp_client, tmp_path):
    client, _ = await make_client(aiohttp_client, tmp_path)

    response = await client.post("/xiaozhi/incident/nope-404/diagnose")

    assert response.status == 404


@pytest.mark.asyncio
async def test_diagnose_alert_relay_source_returns_400_with_reason(
    aiohttp_client, tmp_path
):
    relay = FakeAlertRelay([relay_record(alert_id="relay-1")])
    client, _ = await make_client(aiohttp_client, tmp_path, alert_relay=relay)

    response = await client.post("/xiaozhi/incident/relay-1/diagnose")

    assert response.status == 400
    body = await response.json()
    assert body["success"] is False
    assert "值班中继" in body["message"]


@pytest.mark.asyncio
async def test_diagnose_disk_only_record_writes_back(aiohttp_client, tmp_path):
    """重启后只剩落盘时间线的故障也能复盘诊断，结果写回原文件。"""
    write_disk_record(tmp_path, TODAY, "disk-review-1")
    runner = GatedRunner()
    client, app = await make_client(aiohttp_client, tmp_path, runner=runner)

    response = await client.post("/xiaozhi/incident/disk-review-1/diagnose")

    assert response.status == 200
    runner.gate.set()
    await app["manager"].wait_idle()

    on_disk = json.loads(
        (tmp_path / f"{TODAY}-disk-review-1.json").read_text(encoding="utf-8")
    )
    assert on_disk["diagnosis"]["ok"] is True
    assert on_disk["diagnosis"]["summary"] == "复盘结论：上游超时"
    events = [event["event"] for event in on_disk["timeline"]]
    assert "diagnosis_started" in events
    assert "diagnosis_result" in events

    listed = await (await client.get("/xiaozhi/incident/list")).json()
    assert listed["incidents"][0]["diagnosis"]["state"] == "done"


# ================================================================ 鉴权与路由


@pytest.mark.asyncio
async def test_new_endpoints_require_auth_when_enabled(aiohttp_client, tmp_path):
    client, _ = await make_client(aiohttp_client, tmp_path, auth=True)
    headers = {"Authorization": "Bearer test-secret"}

    assert (await client.get("/xiaozhi/incident/list")).status == 401
    assert (await client.post("/xiaozhi/incident/x/ack")).status == 401
    assert (await client.post("/xiaozhi/incident/x/diagnose")).status == 401
    assert (
        await client.get("/xiaozhi/incident/list", headers=headers)
    ).status == 200


def test_new_routes_are_registered(tmp_path):
    app = build_app(tmp_path)

    signatures = {
        (route.method, route.resource.canonical) for route in app.router.routes()
    }

    assert signatures >= {
        ("GET", "/xiaozhi/incident/list"),
        ("OPTIONS", "/xiaozhi/incident/list"),
        ("POST", "/xiaozhi/incident/{incident_id}/ack"),
        ("OPTIONS", "/xiaozhi/incident/{incident_id}/ack"),
        ("POST", "/xiaozhi/incident/{incident_id}/diagnose"),
        ("OPTIONS", "/xiaozhi/incident/{incident_id}/diagnose"),
    }
