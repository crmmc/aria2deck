import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_uvicorn_commands_disable_access_log() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    makefile = (ROOT / "Makefile").read_text()

    assert "--no-access-log" in dockerfile
    assert all(
        "--no-access-log" in line
        for line in makefile.splitlines()
        if "uvicorn" in line and not line.lstrip().startswith("#")
    )
    assert "/api/health/ready" in dockerfile

    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    start_script = re.search(
        r"cat > dist/aria2deck/start\.sh << [^\n]+\n(?P<script>.*?)^\s*EOF$",
        release,
        re.MULTILINE | re.DOTALL,
    )
    assert start_script is not None
    uvicorn_commands = [
        line.strip()
        for line in start_script.group("script").splitlines()
        if "uvicorn" in line
    ]
    assert len(uvicorn_commands) == 1
    assert "--no-access-log" in uvicorn_commands[0]


def test_docker_7zz_asset_is_versioned_and_verified() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "/v25.01-v1.5.7-R4/linux-gcc-x64.zip" in dockerfile
    assert "b7526802535bf98d6268ce1960de7e36cf8ed6b4004c9ba3ac09db9e14d9a20d" in dockerfile
    assert "sha256sum -c -" in dockerfile


def test_workflows_pin_actions_and_release_avoids_remote_shell() -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    workflows = list(workflow_dir.glob("*.yml"))
    assert workflows

    for workflow in workflows:
        text = workflow.read_text()
        for line in text.splitlines():
            if "uses:" in line:
                assert re.search(r"@[0-9a-f]{40}\s+# v\d", line), line

    release = (workflow_dir / "release.yml").read_text()
    assert not re.search(r"curl[^\n]*\|[^\n]*sh", release)
    assert "uv is required to run this package" in release
