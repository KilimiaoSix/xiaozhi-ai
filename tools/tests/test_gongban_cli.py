"""统一启动器 gongban 的行为测试。

这里只覆盖**不会碰到任何真实进程**的分支：用法、未知子命令、demo 分发、
以及"换个 cwd 也能自定位仓库根"。up / down / status / doctor 会去动真实
Server 与桌面应用，按仓库约定交人工验收，不在自动化测试里跑。
"""

import json
import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GONGBAN = REPO_ROOT / "gongban"
TOOLS_DIR = REPO_ROOT / "tools"

SUBCOMMANDS = ("up", "down", "status", "doctor", "demo", "mock-device")


def run(args, cwd="/"):
    """故意在仓库外的 cwd 跑：脚本必须靠 dirname 自定位，不能依赖当前目录。"""
    return subprocess.run(
        [str(GONGBAN)] + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_gongban_exists_and_is_executable():
    assert GONGBAN.is_file(), f"缺少启动器：{GONGBAN}"
    assert os.access(GONGBAN, os.X_OK), "gongban 必须可执行（chmod +x）"


def test_usage_lists_every_subcommand():
    result = run(["help"])
    assert result.returncode == 0, result.stderr
    for name in SUBCOMMANDS:
        assert name in result.stdout, f"用法里缺少子命令 {name}"


def test_no_args_prints_usage_and_exits_2():
    result = run([])
    assert result.returncode == 2
    assert "用法" in result.stderr


def test_unknown_subcommand_exits_2():
    result = run(["definitely-not-a-command"])
    assert result.returncode == 2
    assert "definitely-not-a-command" in result.stdout + result.stderr


def test_demo_without_scenario_lists_scenarios():
    result = run(["demo"])
    assert result.returncode == 2
    text = result.stdout + result.stderr
    assert "incident" in text
    assert "away-return" in text


def test_demo_unknown_scenario_exits_2():
    result = run(["demo", "no-such-scene"])
    assert result.returncode == 2
    assert "no-such-scene" in result.stdout + result.stderr


def test_demo_dispatches_to_ported_script_from_foreign_cwd():
    # demo-incident.sh 不带动作时只打印自己的用法头、退出 1，不碰网络。
    # 用它同时验证两件事：分发到了 tools/ 下的移植版，且仓库根是自定位出来的。
    result = run(["demo", "incident"], cwd="/")
    assert result.returncode == 1
    assert "流程七" in result.stdout + result.stderr


def test_demo_away_return_dispatches():
    result = run(["demo", "away-return"], cwd="/")
    assert result.returncode == 1
    assert "流程四" in result.stdout + result.stderr


def test_ported_demo_scripts_have_no_hardcoded_user_paths():
    for name in ("demo-incident.sh", "demo-away-return.sh"):
        path = TOOLS_DIR / name
        assert path.is_file(), f"缺少移植版演示脚本：{path}"
        assert os.access(path, os.X_OK), f"{name} 必须可执行"
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text, f"{name} 里还留着写死的绝对路径"


def _source_gongban(cwd, env_overrides, snippet):
    """把 gongban 当函数库 source 进来跑一小段代码，不触发底部的 case 分发。

    依赖 gongban 自己用 `[ "${BASH_SOURCE[0]}" = "${0}" ]` 守住分发块——
    source 时 $0 是调用方（bash）的 $0，不等于被 source 的文件路径，分发块因此跳过。
    """
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["bash", "-c", f'source "{GONGBAN}"; {snippet}'],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def test_find_desktop_app_picks_newest_by_mtime_not_glob_order(tmp_path):
    # 复刻真实问题：desktop/out 下同时留着旧产物「小飞…」与新产物「小智…」，
    # 按名字排序旧的在前；不少 locale（如 en_US.UTF-8/zh_CN.UTF-8）下 glob 就是这个顺序。
    out_dir = tmp_path / "out"
    old_app = out_dir / "小飞桌面机器人-darwin-arm64" / "小飞桌面机器人.app"
    new_app = out_dir / "小智桌面机器人-darwin-arm64" / "小智桌面机器人.app"
    old_app.mkdir(parents=True)
    new_app.mkdir(parents=True)
    now = time.time()
    os.utime(old_app, (now - 3600, now - 3600))
    os.utime(new_app, (now, now))

    result = _source_gongban(
        "/",
        {"GONGBAN_DESKTOP_OUT_DIR": str(out_dir), "LC_ALL": "en_US.UTF-8"},
        "find_desktop_app",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(new_app), result.stdout


def test_find_desktop_app_prefers_product_name_over_newer_mtime(tmp_path):
    # productName 命中要比 mtime 优先：即使命中的产物 mtime 更旧，也不能被一个
    # 同目录下、名字对不上 productName 但 mtime 更新的残留产物抢走。
    out_dir = tmp_path / "out"
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps({"productName": "测试产品名"}), encoding="utf-8"
    )

    named_app = out_dir / "测试产品名-darwin-arm64" / "测试产品名.app"
    newer_but_wrong_name_app = out_dir / "旧构建-darwin-arm64" / "旧构建.app"
    named_app.mkdir(parents=True)
    newer_but_wrong_name_app.mkdir(parents=True)
    now = time.time()
    os.utime(named_app, (now - 3600, now - 3600))
    os.utime(newer_but_wrong_name_app, (now, now))

    result = _source_gongban(
        "/",
        {
            "GONGBAN_DESKTOP_OUT_DIR": str(out_dir),
            "GONGBAN_DESKTOP_PACKAGE_JSON": str(package_json),
        },
        "find_desktop_app",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(named_app), result.stdout
    assert result.stderr.strip() == "", "productName 命中不该走 mtime 的多候选警告"


def test_find_desktop_app_falls_back_to_mtime_when_product_name_has_no_app(tmp_path):
    # productName 读得到，但 out 目录下没有同名 .app（比如刚改过打包名还没重新
    # 打包）：不能直接判失败，要退回 mtime 兜底。
    out_dir = tmp_path / "out"
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps({"productName": "不存在的产品名"}), encoding="utf-8"
    )

    old_app = out_dir / "小飞桌面机器人-darwin-arm64" / "小飞桌面机器人.app"
    new_app = out_dir / "小智桌面机器人-darwin-arm64" / "小智桌面机器人.app"
    old_app.mkdir(parents=True)
    new_app.mkdir(parents=True)
    now = time.time()
    os.utime(old_app, (now - 3600, now - 3600))
    os.utime(new_app, (now, now))

    result = _source_gongban(
        "/",
        {
            "GONGBAN_DESKTOP_OUT_DIR": str(out_dir),
            "GONGBAN_DESKTOP_PACKAGE_JSON": str(package_json),
        },
        "find_desktop_app",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(new_app), result.stdout


def test_find_desktop_app_falls_back_to_mtime_when_package_json_missing(tmp_path):
    # package.json 读不到（路径不存在/解析失败）时同样要退回 mtime 兜底，
    # 不能因为读不到 productName 就直接判失败。
    out_dir = tmp_path / "out"
    old_app = out_dir / "小飞桌面机器人-darwin-arm64" / "小飞桌面机器人.app"
    new_app = out_dir / "小智桌面机器人-darwin-arm64" / "小智桌面机器人.app"
    old_app.mkdir(parents=True)
    new_app.mkdir(parents=True)
    now = time.time()
    os.utime(old_app, (now - 3600, now - 3600))
    os.utime(new_app, (now, now))

    result = _source_gongban(
        "/",
        {
            "GONGBAN_DESKTOP_OUT_DIR": str(out_dir),
            "GONGBAN_DESKTOP_PACKAGE_JSON": str(tmp_path / "no-such-package.json"),
        },
        "find_desktop_app",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(new_app), result.stdout


def test_find_desktop_app_warns_on_stale_candidates():
    out_dir_env = "GONGBAN_DESKTOP_OUT_DIR"

    def make_out_dir(tmp_path):
        out_dir = tmp_path / "out"
        old_app = out_dir / "old-darwin-arm64" / "旧.app"
        new_app = out_dir / "new-darwin-arm64" / "新.app"
        old_app.mkdir(parents=True)
        new_app.mkdir(parents=True)
        now = time.time()
        os.utime(old_app, (now - 3600, now - 3600))
        os.utime(new_app, (now, now))
        return out_dir

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = make_out_dir(Path(tmp))
        result = _source_gongban(
            "/", {out_dir_env: str(out_dir)}, "find_desktop_app"
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr.strip(), "多个候选 .app 时应该向 stderr 发 warn 提示清理旧产物"


def test_find_desktop_app_single_candidate_no_warning(tmp_path):
    out_dir = tmp_path / "out"
    app = out_dir / "小智桌面机器人-darwin-arm64" / "小智桌面机器人.app"
    app.mkdir(parents=True)

    result = _source_gongban(
        "/", {"GONGBAN_DESKTOP_OUT_DIR": str(out_dir)}, "find_desktop_app"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(app)
    assert result.stderr.strip() == ""


def test_find_desktop_app_no_candidate_returns_1(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = _source_gongban(
        "/", {"GONGBAN_DESKTOP_OUT_DIR": str(out_dir)}, "find_desktop_app"
    )
    assert result.returncode == 1


def test_ported_demo_scripts_keep_env_var_semantics():
    incident = (TOOLS_DIR / "demo-incident.sh").read_text(encoding="utf-8")
    assert "DESKPET_SERVER" in incident
    assert "XZ_DIR" in incident

    away = (TOOLS_DIR / "demo-away-return.sh").read_text(encoding="utf-8")
    assert "DESKPET_SERVER" in away
    assert "DESKPET_DEVICE_ID" in away
    assert "DESKPET_LEDGER" in away
