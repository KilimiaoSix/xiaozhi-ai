from hashlib import sha256
import os
from pathlib import Path
from shutil import which
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "presence-agent"
MODEL_HASH = "59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a"
BASH = which("bash")
SHELL_SCRIPTS = (
    AGENT_ROOT / "setup.sh",
    AGENT_ROOT / "run.sh",
    AGENT_ROOT / "enroll-face.sh",
    REPO_ROOT / "run-presence-stack.sh",
)


def lines(path):
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_runtime_and_test_dependencies_are_exactly_pinned():
    assert lines(AGENT_ROOT / "requirements.txt") == {
        "aiohttp==3.13.2",
        "mediapipe==1.0.1",
        "numpy==2.5.2",
        "opencv-contrib-python==5.0.0.93",
    }
    assert lines(AGENT_ROOT / "requirements-test.txt") == {
        "-r requirements.txt",
        "pytest==9.1.1",
        "pytest-aiohttp==1.1.0",
    }


def test_bundled_model_matches_validated_demo():
    model = AGENT_ROOT / "models" / "pose_landmarker_lite.task"

    assert model.stat().st_size == 5_777_746
    assert sha256(model.read_bytes()).hexdigest() == MODEL_HASH


def test_bundled_face_models_and_licenses_match_validated_demo():
    yunet = AGENT_ROOT / "models" / "face_detection_yunet_2026may.onnx"
    sface = AGENT_ROOT / "models" / "face_recognition_sface_2021dec.onnx"

    assert yunet.stat().st_size == 229_738
    assert sface.stat().st_size == 38_696_353
    assert sha256(yunet.read_bytes()).hexdigest() == (
        "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0"
    )
    assert sha256(sface.read_bytes()).hexdigest() == (
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
    )
    assert "MIT License" in (AGENT_ROOT / "models" / "YUNET_LICENSE.txt").read_text(
        encoding="utf-8"
    )
    assert "Apache License" in (
        AGENT_ROOT / "models" / "SFACE_LICENSE.txt"
    ).read_text(encoding="utf-8")


@pytest.mark.skipif(
    which("powershell") is None and which("pwsh") is None,
    reason="PowerShell 不在当前平台上（macOS/Linux 用 *.sh 入口）",
)
def test_launch_scripts_parse_as_powershell():
    shell = which("powershell") or which("pwsh")
    for script in (
        AGENT_ROOT / "setup.ps1",
        AGENT_ROOT / "run.ps1",
        AGENT_ROOT / "enroll-face.ps1",
        REPO_ROOT / "run-presence-stack.ps1",
    ):
        command = (
            "[void][scriptblock]::Create((Get-Content -Raw -LiteralPath "
            f"'{script}'))"
        )
        result = subprocess.run(
            [shell, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.skipif(BASH is None, reason="bash 不在当前平台上（Windows 用 *.ps1 入口）")
def test_shell_launch_scripts_parse_as_bash():
    for script in SHELL_SCRIPTS:
        result = subprocess.run(
            [BASH, "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_shell_launch_scripts_are_executable():
    for script in SHELL_SCRIPTS:
        assert script.is_file()
        assert os.access(script, os.X_OK), f"{script.name} 需要可执行权限"


def test_shell_entry_points_expose_smoke_frames_as_named_parameter():
    for script in (AGENT_ROOT / "run.sh", REPO_ROOT / "run-presence-stack.sh"):
        content = script.read_text(encoding="utf-8")
        assert "--smoke-frames) SMOKE_FRAMES=" in content
        assert 'SMOKE_FRAMES=0' in content


def test_shell_root_launcher_exposes_face_lifecycle_and_tuning_parameters():
    content = (REPO_ROOT / "run-presence-stack.sh").read_text(encoding="utf-8")

    for expected in (
        "--enroll-owner",
        "--delete-face-template",
        "--force-enrollment",
        "--disable-face-verification",
        "FACE_THRESHOLD=0.45",
        "FACE_HITS=3",
        "NO_FACE_DELAY=1.0",
    ):
        assert expected in content


def test_shell_enrollment_script_returns_to_root_launcher_after_success():
    content = (AGENT_ROOT / "enroll-face.sh").read_text(encoding="utf-8")

    assert "ENROLLMENT_EXIT_CODE=$?" in content
    assert 'if [ "$ENROLLMENT_EXIT_CODE" -ne 0 ]; then' in content


def test_shell_template_deletion_happens_before_environment_setup():
    content = (REPO_ROOT / "run-presence-stack.sh").read_text(encoding="utf-8")

    assert content.index('if [ "$DELETE_FACE_TEMPLATE" -eq 1 ]') < content.index(
        '"$AGENT_ROOT/setup.sh" --python'
    )


def test_shell_setup_rejects_interpreters_older_than_the_numpy_pin():
    content = (AGENT_ROOT / "setup.sh").read_text(encoding="utf-8")

    assert "MIN_PYTHON_MINOR=12" in content
    assert "sys.version_info >= (3, $MIN_PYTHON_MINOR)" in content


def test_shell_root_launcher_can_actually_stop_the_server_it_started():
    # 没有 exec 时 $! 是子 shell 的 PID，trap 杀掉它以后 Server 仍占着端口。
    content = (REPO_ROOT / "run-presence-stack.sh").read_text(encoding="utf-8")

    assert 'exec "$SERVER_PYTHON" app.py' in content
    assert 'exec "$AGENT_PYTHON" presence_server.py' in content
    assert 'STARTED_SERVER_PID=$!' in content
    assert 'kill "$STARTED_SERVER_PID"' in content


@pytest.mark.skipif(BASH is None, reason="bash 不在当前平台上（Windows 用 *.ps1 入口）")
def test_shell_health_check_survives_set_u_without_auth_token(tmp_path):
    # macOS 自带 bash 3.2：set -u 下展开空数组会直接报 unbound variable。
    script = (REPO_ROOT / "run-presence-stack.sh").read_text(encoding="utf-8")
    body = script[
        script.index("presence_server_ready() {") : script.index("wait_presence_server() {")
    ]
    harness = tmp_path / "health.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'SERVER_URL="http://127.0.0.1:8003"\n'
        "curl() { echo 404; }\n"
        f"{body}\n"
        "presence_server_ready && echo READY\n",
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items()}
    environment.pop("PRESENCE_AUTH_TOKEN", None)

    result = subprocess.run(
        [BASH, str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert "unbound variable" not in result.stderr
    assert result.returncode == 0, result.stderr
    assert "READY" in result.stdout


@pytest.mark.skipif(BASH is None, reason="bash 不在当前平台上（Windows 用 *.ps1 入口）")
def test_shell_endpoint_resolution_keeps_url_host_and_port_consistent(tmp_path):
    # 健康检查用 SERVER_URL、启动 Server 用 SERVER_HOST/SERVER_PORT、agent 又用 SERVER_URL，
    # 三者必须指向同一个地址，否则等待 30 秒后报“Server 没就绪”而 Server 其实是好的。
    script = (REPO_ROOT / "run-presence-stack.sh").read_text(encoding="utf-8")
    body = script[
        script.index("resolve_server_endpoint() {") : script.index("presence_server_ready() {")
    ]
    harness = tmp_path / "endpoint.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"{body}\n"
        'for url in "$@"; do\n'
        '    SERVER_URL="$url"\n'
        "    resolve_server_endpoint\n"
        '    echo "$SERVER_URL|$SERVER_HOST|$SERVER_PORT"\n'
        "done\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            BASH,
            str(harness),
            "http://127.0.0.1:8003",
            "http://localhost",
            "http://127.0.0.1/",
            "https://server-host",
            "http://[::1]:8003",
            "http://[::1]",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "http://127.0.0.1:8003|127.0.0.1|8003",
        "http://localhost:80|localhost|80",
        "http://127.0.0.1:80|127.0.0.1|80",
        "https://server-host:443|server-host|443",
        "http://[::1]:8003|::1|8003",
        "http://[::1]:80|::1|80",
    ]


def test_shell_scripts_do_not_embed_example_secrets():
    scripts = "\n".join(path.read_text(encoding="utf-8") for path in SHELL_SCRIPTS)

    assert "test-secret" not in scripts
    assert "Authorization: Bearer" not in scripts
    assert "--auth-token" not in (AGENT_ROOT / "run.sh").read_text(encoding="utf-8")


def test_powershell_entry_points_expose_smoke_frames_as_named_parameter():
    for script in (AGENT_ROOT / "run.ps1", REPO_ROOT / "run-presence-stack.ps1"):
        content = script.read_text(encoding="utf-8")
        assert "[int]$SmokeFrames = 0" in content
        assert "ValueFromRemainingArguments" not in content


def test_root_launcher_exposes_face_lifecycle_and_tuning_parameters():
    content = (REPO_ROOT / "run-presence-stack.ps1").read_text(encoding="utf-8")

    for expected in (
        "[switch]$EnrollOwner",
        "[switch]$DeleteFaceTemplate",
        "[double]$FaceThreshold = 0.45",
        "[int]$FaceHits = 3",
        "[double]$NoFaceDelay = 1.0",
    ):
        assert expected in content


def test_enrollment_script_returns_to_root_launcher_after_success():
    content = (AGENT_ROOT / "enroll-face.ps1").read_text(encoding="utf-8")

    assert "exit $LASTEXITCODE" not in content
    assert "if ($enrollmentExitCode -ne 0)" in content


def test_template_deletion_happens_before_environment_setup():
    content = (REPO_ROOT / "run-presence-stack.ps1").read_text(encoding="utf-8")

    assert content.index("if ($DeleteFaceTemplate)") < content.index(
        '"setup.ps1") -PythonExe'
    )


def test_setup_callers_do_not_treat_missing_last_exit_code_as_failure():
    for script in (AGENT_ROOT / "run.ps1", REPO_ROOT / "run-presence-stack.ps1"):
        content = script.read_text(encoding="utf-8")
        assert "if (-not $?)" in content
        assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" not in content


def test_runtime_directories_are_ignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "presence-agent/.venv/" in gitignore
    assert "presence-agent/.runtime/" in gitignore


def test_readme_documents_real_entry_points_and_privacy_boundary():
    readme = (AGENT_ROOT / "README.md").read_text(encoding="utf-8")

    for expected in (
        ".\\run.ps1",
        "--server-url",
        "--workstation-id",
        "-SmokeFrames 30",
        "PRESENCE_AUTH_TOKEN",
        "不会上传",
    ):
        assert expected in readme


def test_readme_documents_the_macos_entry_points_and_prerequisites():
    readme = (AGENT_ROOT / "README.md").read_text(encoding="utf-8")

    for expected in (
        "./run.sh",
        "./enroll-face.sh",
        "../run-presence-stack.sh",
        "Python 3.12+",
        "隐私与安全性 > 摄像头",
        "Metal",
    ):
        assert expected in readme

    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "./run-presence-stack.sh --workstation-id" in root_readme


def test_scripts_do_not_embed_example_secrets():
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            AGENT_ROOT / "setup.ps1",
            AGENT_ROOT / "run.ps1",
            REPO_ROOT / "run-presence-stack.ps1",
        )
    )

    assert "test-secret" not in scripts
    assert "Authorization: Bearer" not in scripts
    assert '"--auth-token"' not in (AGENT_ROOT / "run.ps1").read_text(encoding="utf-8")
