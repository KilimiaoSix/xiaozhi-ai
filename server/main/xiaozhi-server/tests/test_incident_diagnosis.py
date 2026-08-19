"""无头诊断执行器测试（需求文档流程七）。

不真的跑 claude：子进程 spawn 全部注入假实现，覆盖成功 / 超时 / 非零退出 /
坏 JSON 四条失败面。成功那条用的是 claude CLI 2.1.229 实测的输出结构
（见 core/incident_diagnosis.py 模块头注释）。
"""

import asyncio
import json

import pytest

from core.incident_diagnosis import (
    DEFAULT_ALLOWED_TOOLS,
    MAX_SUMMARY_CHARS,
    DiagnosisRunner,
)

pytestmark = pytest.mark.asyncio


INCIDENT = {
    "incident_id": "demo-api-abc12345",
    "service": "demo-api",
    "severity": "P1",
    "title": "接口错误率升高",
    "message": "支付回调错误率 12%",
    "metric": "error_rate",
    "value": "12%",
    "started_at": "2026-08-19T10:00:00",
    "repeat_count": 3,
    "simulated": True,
}


def cli_json(result_text: str, **overrides) -> bytes:
    """claude -p --output-format json 的实测结构（截取相关字段）。"""
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
        "session_id": "d961ba88-22cf-44c4-8327-8300aaaa11ab",
        "num_turns": 1,
        "stop_reason": "end_turn",
        "duration_ms": 8255,
        "total_cost_usd": 0.2516,
        "permission_denials": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self._killed = asyncio.Event()
        self.killed = False

    async def communicate(self):
        if self._hang:
            await self._killed.wait()
            raise asyncio.CancelledError()
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._killed.set()

    async def wait(self):
        return self.returncode


class SpawnRecorder:
    def __init__(self, process=None, error=None):
        self.process = process or FakeProcess(cli_json("一切正常"))
        self.error = error
        self.calls = []

    async def __call__(self, argv, cwd, env):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env})
        if self.error is not None:
            raise self.error
        return self.process

    @property
    def argv(self):
        return self.calls[0]["argv"]

    @property
    def prompt(self):
        return self.calls[0]["argv"][2]


def build_runner(spawn, **incident_config):
    config = {"incident": {"timeout_s": 5, **incident_config}}
    return DiagnosisRunner(config, spawn=spawn)


# ---------------------------------------------------------------- 成功路径


async def test_success_returns_summary_from_result_field():
    spawn = SpawnRecorder(FakeProcess(cli_json("根因是上游超时。最近变更是昨天的发布。建议回滚。")))
    runner = build_runner(spawn)

    result = await runner.run(INCIDENT)

    assert result["ok"] is True
    assert result["summary"] == "根因是上游超时。最近变更是昨天的发布。建议回滚。"
    assert result["raw"]["session_id"] == "d961ba88-22cf-44c4-8327-8300aaaa11ab"


async def test_multiline_summary_is_flattened_for_tts():
    spawn = SpawnRecorder(FakeProcess(cli_json("1) 上游超时\n2) 昨天发布\n3)  建议回滚")))
    runner = build_runner(spawn)

    result = await runner.run(INCIDENT)

    assert result["summary"] == "1) 上游超时 2) 昨天发布 3) 建议回滚"


async def test_overlong_summary_is_truncated():
    spawn = SpawnRecorder(FakeProcess(cli_json("根因" * 400)))
    runner = build_runner(spawn)

    result = await runner.run(INCIDENT)

    assert len(result["summary"]) == MAX_SUMMARY_CHARS + 1  # 含省略号


# ---------------------------------------------------------------- 命令与环境


async def test_argv_uses_readonly_tools_and_json_output():
    spawn = SpawnRecorder()
    runner = build_runner(spawn, claude_bin="/usr/local/bin/claude", allowed_tools="Read Grep")

    await runner.run(INCIDENT)

    argv = spawn.argv
    assert argv[0] == "/usr/local/bin/claude"
    assert argv[1] == "-p"
    assert argv[3:] == ["--output-format", "json", "--allowedTools", "Read Grep"]


async def test_allowed_tools_accepts_list_and_defaults():
    list_spawn = SpawnRecorder()
    await build_runner(list_spawn, allowed_tools=["Read", "Glob"]).run(INCIDENT)

    default_spawn = SpawnRecorder()
    await DiagnosisRunner({}, spawn=default_spawn).run(INCIDENT)

    assert list_spawn.argv[-1] == "Read Glob"
    assert default_spawn.argv[-1] == DEFAULT_ALLOWED_TOOLS


async def test_prompt_carries_alert_context_and_readonly_constraint():
    spawn = SpawnRecorder()

    await build_runner(spawn).run(INCIDENT)

    prompt = spawn.prompt
    assert "只做只读分析" in prompt
    assert "不要执行任何修改" in prompt
    assert "demo-api" in prompt
    assert "P1" in prompt
    assert "接口错误率升高" in prompt
    assert "error_rate" in prompt
    assert "2026-08-19T10:00:00" in prompt
    assert "模拟告警" in prompt


async def test_prompt_omits_simulated_note_for_real_alerts():
    spawn = SpawnRecorder()

    await build_runner(spawn).run({**INCIDENT, "simulated": False})

    assert "模拟告警" not in spawn.prompt


async def test_child_env_drops_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-server-llm-key")
    monkeypatch.setenv("PATH_MARKER", "kept")
    spawn = SpawnRecorder()

    await build_runner(spawn).run(INCIDENT)

    env = spawn.calls[0]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH_MARKER"] == "kept"


async def test_diagnosis_dir_is_used_as_cwd(tmp_path):
    spawn = SpawnRecorder()

    await build_runner(spawn, diagnosis_dir=str(tmp_path)).run(INCIDENT)

    assert spawn.calls[0]["cwd"] == str(tmp_path)


# ---------------------------------------------------------------- 失败路径


async def test_timeout_kills_the_process():
    process = FakeProcess(hang=True)
    spawn = SpawnRecorder(process)
    runner = DiagnosisRunner({"incident": {"timeout_s": 0.01}}, spawn=spawn)

    result = await runner.run(INCIDENT)

    assert result["ok"] is False
    assert "超时" in result["error"]
    assert process.killed is True


async def test_non_zero_exit_reports_stderr_tail():
    spawn = SpawnRecorder(FakeProcess(b"", b"Error: not logged in", returncode=1))
    runner = build_runner(spawn)

    result = await runner.run(INCIDENT)

    assert result["ok"] is False
    assert "退出码 1" in result["error"]
    assert "not logged in" in result["error"]


async def test_unparseable_stdout_is_reported():
    spawn = SpawnRecorder(FakeProcess(b"claude: something went sideways"))
    runner = build_runner(spawn)

    result = await runner.run(INCIDENT)

    assert result["ok"] is False
    assert "不是合法 JSON" in result["error"]


async def test_error_result_from_cli_is_reported():
    spawn = SpawnRecorder(
        FakeProcess(
            cli_json("Credit balance is too low", is_error=True, subtype="error_during_execution")
        )
    )
    runner = build_runner(spawn)

    result = await runner.run(INCIDENT)

    assert result["ok"] is False
    assert "claude 报错" in result["error"]
    assert "Credit balance" in result["error"]


async def test_empty_result_field_is_reported():
    spawn = SpawnRecorder(FakeProcess(cli_json("")))
    runner = build_runner(spawn)

    result = await runner.run(INCIDENT)

    assert result["ok"] is False
    assert "没有给出结论" in result["error"]


async def test_empty_stdout_is_reported():
    spawn = SpawnRecorder(FakeProcess(b"", b"boom"))
    runner = build_runner(spawn)

    result = await runner.run(INCIDENT)

    assert result["ok"] is False
    assert "没有输出" in result["error"]


async def test_missing_binary_is_reported():
    spawn = SpawnRecorder(error=FileNotFoundError("claude"))
    runner = build_runner(spawn, claude_bin="claude")

    result = await runner.run(INCIDENT)

    assert result["ok"] is False
    assert "找不到 claude 命令" in result["error"]


# ---------------------------------------------------------------- 回调


async def test_on_result_callback_receives_incident_id_and_result():
    seen = []

    async def on_result(incident_id, result):
        seen.append((incident_id, result))

    spawn = SpawnRecorder(FakeProcess(cli_json("磁盘满了")))
    runner = build_runner(spawn)

    returned = await runner.run(INCIDENT, on_result=on_result)

    assert seen == [("demo-api-abc12345", returned)]
    assert returned["summary"] == "磁盘满了"


async def test_constructor_callback_is_used_when_run_has_none():
    seen = []
    spawn = SpawnRecorder()
    runner = DiagnosisRunner(
        {"incident": {}}, spawn=spawn, on_result=lambda incident_id, result: seen.append(incident_id)
    )

    await runner.run(INCIDENT)

    assert seen == ["demo-api-abc12345"]


# ---------------------------------------------------------------- 模型选择
#
# 诊断子进程默认吃 claude CLI 的全局默认模型。真机 2026-08-19 演示中途撞上
# 「You've reached your Fable 5 limit」（HTTP 429，退出码 1，stderr 为空），
# 整条诊断哑掉。给它一个独立的模型配置，诊断就不再被主力模型的额度拖死。


async def test_no_model_flag_by_default():
    """不配就不传 --model，保持吃 CLI 全局默认，行为与接入前一致。"""
    spawn = SpawnRecorder(FakeProcess(cli_json("结论。")))
    await build_runner(spawn).run(INCIDENT)
    assert "--model" not in spawn.argv


async def test_model_flag_passed_when_configured():
    spawn = SpawnRecorder(FakeProcess(cli_json("结论。")))
    await build_runner(spawn, claude_model="claude-opus-5").run(INCIDENT)
    argv = spawn.argv
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-5"


async def test_blank_model_is_ignored():
    """配成空串/空白按没配处理，别把空参数塞给 CLI。"""
    spawn = SpawnRecorder(FakeProcess(cli_json("结论。")))
    await build_runner(spawn, claude_model="   ").run(INCIDENT)
    assert "--model" not in spawn.argv
