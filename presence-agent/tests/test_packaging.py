from hashlib import sha256
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "presence-agent"
MODEL_HASH = "59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a"


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


def test_launch_scripts_parse_as_powershell():
    for script in (AGENT_ROOT / "setup.ps1", AGENT_ROOT / "run.ps1", REPO_ROOT / "run-presence-stack.ps1"):
        command = (
            "[void][scriptblock]::Create((Get-Content -Raw -LiteralPath "
            f"'{script}'))"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_powershell_entry_points_expose_smoke_frames_as_named_parameter():
    for script in (AGENT_ROOT / "run.ps1", REPO_ROOT / "run-presence-stack.ps1"):
        content = script.read_text(encoding="utf-8")
        assert "[int]$SmokeFrames = 0" in content
        assert "ValueFromRemainingArguments" not in content


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
