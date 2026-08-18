from hashlib import sha256
from pathlib import Path
import shutil
import subprocess

import pytest


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
        "mediapipe==0.10.35",
        "numpy==1.26.4",
        "opencv-contrib-python==4.11.0.86",
    }
    assert lines(AGENT_ROOT / "requirements-test.txt") == {
        "-r requirements.txt",
        "pytest==9.1.1",
        "pytest-aiohttp==1.1.0",
    }


def test_presence_agent_has_installable_package_metadata():
    pyproject = (AGENT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'name = "launchcrush-presence-agent"' in pyproject
    assert 'requires-python = ">=3.10"' in pyproject
    for requirement in lines(AGENT_ROOT / "requirements.txt"):
        assert f'"{requirement}"' in pyproject

    camera_requirements = (
        REPO_ROOT / "server" / "main" / "xiaozhi-server" / "requirements-camera.txt"
    ).read_text(encoding="utf-8")
    assert "-r requirements.txt" not in camera_requirements
    assert "-e ../../../presence-agent" in camera_requirements


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


def test_launch_scripts_parse_as_powershell():
    if shutil.which("powershell") is None:
        pytest.skip("PowerShell is not installed on this platform")

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
