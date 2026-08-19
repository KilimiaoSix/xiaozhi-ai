"""诊断依赖的就绪度自检。

来自真实反馈：把分支拿到另一台 Mac 上，单测和假 CLI 链路都能跑，但真实告警诊断
**空转到超时才失败**——因为它依赖只装在作者 Windows 机器上的个人 skill 和 sae.ps1。
超时失败最难查：看起来像模型慢，实际是依赖压根不存在。所以缺依赖必须**立即失败**
并说清楚缺什么。
"""

import asyncio

import pytest

from core.alert_relay.diagnosis_runner import ClaudeCodeRunner
from core.alert_relay.models import AlertEvent

EVENT = AlertEvent(
    raw_text="告警集群：bj-jxq-autocar",
    level="严重",
    cluster="bj-jxq-autocar",
    workload="iflyplot-ai",
    keyword="无痕改字处理超时",
    project_id="117",
    cluster_id="3",
)


def make_runner(
    tmp_path,
    *,
    with_skill=True,
    with_script=True,
    cli="claude",
    monkeypatch=None,
    **options,
):
    if with_skill:
        skill_dir = tmp_path / ".claude" / "skills" / "diagnose-sae-alert"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# skill", encoding="utf-8")
        if with_script:
            (skill_dir / "scripts").mkdir(exist_ok=True)
            (skill_dir / "scripts" / "sae_logs.py").write_text("# script", encoding="utf-8")
    return ClaudeCodeRunner(cli_command=[cli], cwd=str(tmp_path), **options)


@pytest.fixture
def with_credentials(monkeypatch):
    monkeypatch.setenv("SAE_AUTHORIZATION", "Bearer test-token")
    return True


@pytest.fixture(autouse=True)
def resolvable_cli(monkeypatch):
    monkeypatch.setattr(
        "core.alert_relay.diagnosis_runner.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "claude" else None,
    )


@pytest.fixture
def without_personal_skill(tmp_path, monkeypatch):
    """把 HOME 指到空目录，模拟「别人的电脑」——没有作者的个人 skill。

    不隔离的话这些用例在作者机器上会假绿：个人 skill 就在 ~/.claude/skills 下。
    """
    empty_home = tmp_path / "other-machine-home"
    empty_home.mkdir()
    monkeypatch.setattr(
        "core.alert_relay.diagnosis_runner.Path.home", classmethod(lambda cls: empty_home)
    )
    return empty_home


def problems_of(runner):
    return {item.name: item for item in runner.preflight()}


def test_everything_present_reports_ready(tmp_path, with_credentials):
    runner = make_runner(tmp_path)
    assert runner.ready() is True
    blocking_failures = [
        item.name for item in runner.preflight() if item.blocking and not item.ok
    ]
    assert blocking_failures == []


def test_missing_cli_is_a_blocking_problem(tmp_path, with_credentials):
    runner = make_runner(tmp_path, cli="claude-not-installed", fast_mode=False)
    problem = problems_of(runner)["claude_cli"]
    assert problem.ok is False
    assert problem.blocking is True
    assert "claude-not-installed" in problem.detail


def test_fast_mode_does_not_require_claude_cli(tmp_path, with_credentials):
    runner = make_runner(tmp_path, cli="claude-not-installed")
    problem = problems_of(runner)["claude_cli"]
    assert problem.ok is True
    assert problem.blocking is False
    assert "快速模式" in problem.detail
    assert runner.ready() is True


def test_missing_skill_is_blocking_and_names_where_it_should_live(
    tmp_path, with_credentials, without_personal_skill
):
    """skill 不在，agent 会瞎查一通再超时——不如立刻说清楚。"""
    runner = make_runner(tmp_path, with_skill=False)
    problem = problems_of(runner)["skill"]
    assert problem.ok is False
    assert problem.blocking is True
    assert ".claude/skills/diagnose-sae-alert" in problem.detail.replace("\\", "/")


def test_skill_in_the_repo_counts_even_without_a_personal_copy(tmp_path, with_credentials):
    """别人克隆仓库时没有个人 skill，仓库自带的这份必须被认出来。"""
    runner = make_runner(tmp_path)
    assert problems_of(runner)["skill"].ok is True
    assert "skills" in problems_of(runner)["skill"].detail


def test_fast_mode_requires_the_bundled_log_script(tmp_path, with_credentials):
    runner = make_runner(tmp_path, with_script=False)
    problem = problems_of(runner)["skill"]
    assert problem.ok is False
    assert problem.blocking is True
    assert "sae_logs.py" in problem.detail


def test_missing_sae_credentials_is_blocking(tmp_path, monkeypatch):
    monkeypatch.delenv("SAE_AUTHORIZATION", raising=False)
    monkeypatch.delenv("SAE_COOKIE", raising=False)
    monkeypatch.setattr(
        "core.alert_relay.diagnosis_runner.Path.home", classmethod(lambda cls: tmp_path / "nohome")
    )
    problem = problems_of(make_runner(tmp_path))["sae_credentials"]
    assert problem.ok is False
    assert problem.blocking is True
    assert "SAE_AUTHORIZATION" in problem.detail


def test_credentials_file_counts_as_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("SAE_AUTHORIZATION", raising=False)
    monkeypatch.delenv("SAE_COOKIE", raising=False)
    home = tmp_path / "home"
    (home / ".sae").mkdir(parents=True)
    (home / ".sae" / "sae-token.env").write_text(
        "# comment\nSAE_AUTHORIZATION=Bearer from-file\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "core.alert_relay.diagnosis_runner.Path.home", classmethod(lambda cls: home)
    )
    assert problems_of(make_runner(tmp_path))["sae_credentials"].ok is True


def test_empty_credentials_file_does_not_count(tmp_path, monkeypatch):
    """只有注释的 env 文件等于没配，不能算就绪。"""
    monkeypatch.delenv("SAE_AUTHORIZATION", raising=False)
    monkeypatch.delenv("SAE_COOKIE", raising=False)
    home = tmp_path / "home"
    (home / ".sae").mkdir(parents=True)
    (home / ".sae" / "sae-token.env").write_text("# 这里本来该有 token\n", encoding="utf-8")
    monkeypatch.setattr(
        "core.alert_relay.diagnosis_runner.Path.home", classmethod(lambda cls: home)
    )
    assert problems_of(make_runner(tmp_path))["sae_credentials"].ok is False


def test_missing_source_dirs_warns_but_does_not_block(tmp_path, with_credentials):
    """没挂源码诊断会变弱（why.code 给不出 file:line），但日志那条腿还在，不该拦。"""
    problem = problems_of(make_runner(tmp_path, fast_mode=False))["source_dirs"]
    assert problem.ok is False
    assert problem.blocking is False
    assert make_runner(tmp_path, fast_mode=False).ready() is True


def test_fast_mode_does_not_warn_about_source_dirs(tmp_path, with_credentials):
    problem = problems_of(make_runner(tmp_path))["source_dirs"]
    assert problem.ok is True
    assert problem.blocking is False
    assert "快速模式" in problem.detail


def test_source_dir_that_does_not_exist_is_reported(tmp_path, with_credentials):
    runner = make_runner(
        tmp_path, source_dirs=[str(tmp_path / "nope")], fast_mode=False
    )
    problem = problems_of(runner)["source_dirs"]
    assert problem.ok is False
    assert "nope" in problem.detail


@pytest.mark.asyncio
async def test_run_fails_fast_instead_of_burning_the_timeout(tmp_path, monkeypatch):
    """这是这次真实反馈的核心：不能空转到 900 秒超时才说不行。"""
    monkeypatch.delenv("SAE_AUTHORIZATION", raising=False)
    monkeypatch.delenv("SAE_COOKIE", raising=False)
    monkeypatch.setattr(
        "core.alert_relay.diagnosis_runner.Path.home", classmethod(lambda cls: tmp_path / "nohome")
    )
    spawned = []

    async def spawn(argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("依赖没就绪就不该起子进程")

    runner = ClaudeCodeRunner(
        cli_command=["claude"], cwd=str(tmp_path), spawn=spawn, timeout_seconds=900
    )
    result = await asyncio.wait_for(runner.run(EVENT), timeout=5)

    assert result.ok is False
    assert "依赖未就绪" in result.reason
    assert "SAE_AUTHORIZATION" in result.detail
    assert spawned == []


@pytest.mark.asyncio
async def test_run_proceeds_when_only_non_blocking_items_are_missing(tmp_path, with_credentials):
    """源码没挂只是降级，不能因此拒跑——日志那条腿还能查出东西。"""
    started = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            started.append(True)
            return b'{"type":"result","is_error":false,"result":"{\\"title\\":\\"t\\",\\"root_cause\\":\\"r\\"}"}', b""

    async def spawn(argv, **kwargs):
        return FakeProcess()

    runner = make_runner(tmp_path, spawn=spawn, fast_mode=False)
    result = await runner.run(EVENT)
    assert result.ok is True
    assert started == [True]


def test_health_exposes_readiness_for_remote_checking(
    tmp_path, with_credentials, without_personal_skill
):
    """别人换机器部署时，靠 /xiaozhi/alert/health 就能看出缺什么，不用等告警来。"""
    health = make_runner(tmp_path, with_skill=False).health()
    assert health["ready"] is False
    assert any("skill" == item["name"] for item in health["prerequisites"])
    assert any(item["blocking"] and not item["ok"] for item in health["prerequisites"])


@pytest.mark.asyncio
async def test_offline_fake_cli_can_explicitly_skip_external_preflight(tmp_path, monkeypatch):
    """假 CLI 只验证管道，不能反过来要求真实 SAE 凭证和项目 skill。"""
    monkeypatch.delenv("SAE_AUTHORIZATION", raising=False)
    monkeypatch.delenv("SAE_COOKIE", raising=False)
    started = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            started.append(True)
            return b'{"title":"t","root_cause":"r"}', b""

    async def spawn(argv, **kwargs):
        return FakeProcess()

    runner = ClaudeCodeRunner(
        cli_command=["offline-fake-claude"],
        cwd=str(tmp_path),
        spawn=spawn,
        enforce_preflight=False,
    )

    result = await runner.run(EVENT)

    assert result.ok is True
    assert started == [True]
