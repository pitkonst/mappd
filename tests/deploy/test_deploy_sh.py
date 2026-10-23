"""deploy/deploy.sh driven by a fake ssh on PATH: every call is logged, none runs."""

import io
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GIT: str = shutil.which("git") or "git"
SH: str = shutil.which("sh") or "sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("sh") is None,
    reason="deploy.sh needs git and sh",
)

FAKE_SSH = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
cat >> "$FAKE_SSH_LOG.stdin"
if [ -n "${FAKE_SSH_FAIL_ON:-}" ]; then
    case "$*" in *"$FAKE_SSH_FAIL_ON"*) exit "${FAKE_SSH_FAIL_CODE:-1}";; esac
fi
exit 0
"""

FILES = {
    "compose.yaml": "services: {}\n",
    "compose.app.yaml": "services: {}\n",
    "compose.deploy.yaml": "services: {}\n",
    "deploy/agentgateway/x.yaml": "config: true\n",
    "deploy/postgres/init.sql": "select 1;\n",
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with the deploy files and deploy.sh; a fake ssh and its log; PATH set."""
    for name, content in FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    script = tmp_path / "deploy" / "deploy.sh"
    shutil.copy(REPO / "deploy" / "deploy.sh", script)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "init"],
    ):
        subprocess.run(  # noqa: S603 - git on a fixture repo
            [GIT, *args], cwd=tmp_path, check=True, capture_output=True
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.delenv("FAKE_SSH_FAIL_ON", raising=False)
    monkeypatch.delenv("MAPPD_DEPLOY_REGISTRY", raising=False)
    monkeypatch.delenv("MAPPD_DEPLOY_DIR", raising=False)
    return tmp_path


def sha12(repo: Path) -> str:
    return subprocess.run(  # noqa: S603 - git on a fixture repo
        [GIT, "rev-parse", "--short=12", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def run_deploy(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(  # noqa: S603 - sh on the script under test
        [SH, "deploy/deploy.sh", *args],
        cwd=repo,
        env=merged,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def ssh_lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_deploys_the_sha(repo: Path) -> None:
    sha = sha12(repo)
    proc = run_deploy(
        repo,
        "deploy@server",
        "HEAD",
        env={"MAPPD_DEPLOY_REGISTRY": "reg.local", "MAPPD_DEPLOY_DIR": "/srv/mappd"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"deployed {sha}"

    lines = ssh_lines(repo / "ssh.log")
    assert all(line.startswith("-o BatchMode=yes deploy@server") for line in lines)

    # The upload: stdin of the first call is a tar of the release files.
    stdin_tar = (repo / "ssh.log.stdin").read_bytes()
    with tarfile.open(fileobj=io.BytesIO(stdin_tar)) as tar:
        names = tar.getnames()
    assert "compose.yaml" in names
    assert "deploy/postgres/init.sql" in names
    assert "mkdir -p /srv/mappd" in lines[0]

    # release.env pins the release: both images tagged with the same SHA.
    release = lines[1]
    assert f"reg.local/mappd-app:{sha}" in release
    assert f"reg.local/mappd-mlflow:{sha}" in release
    assert "MAPPD_APP_VERSION" in release

    pull_up = next(i for i, line in enumerate(lines) if "up -d --wait" in line)
    assert lines[pull_up].index("pull") < lines[pull_up].index("up -d")
    doctor = next(i for i, line in enumerate(lines) if "mappd doctor" in line)
    assert pull_up < doctor


def test_default_deploy_dir(repo: Path) -> None:
    proc = run_deploy(repo, "host", "HEAD", env={"MAPPD_DEPLOY_REGISTRY": "r"})
    assert proc.returncode == 0, proc.stderr
    assert "mkdir -p /opt/mappd" in ssh_lines(repo / "ssh.log")[0]


def test_a_failed_up_stops_the_deploy(repo: Path) -> None:
    proc = run_deploy(
        repo, "host", "HEAD", env={"MAPPD_DEPLOY_REGISTRY": "r", "FAKE_SSH_FAIL_ON": "up -d"}
    )
    assert proc.returncode != 0
    assert "deployed" not in proc.stdout
    assert not any("mappd doctor" in line for line in ssh_lines(repo / "ssh.log"))


def test_a_failing_doctor_is_a_warning(repo: Path) -> None:
    sha = sha12(repo)
    proc = run_deploy(
        repo,
        "host",
        "HEAD",
        env={"MAPPD_DEPLOY_REGISTRY": "r", "FAKE_SSH_FAIL_ON": "mappd doctor"},
    )
    assert proc.returncode == 0
    assert f"deployed {sha}" in proc.stdout
    assert "doctor reports problems" in proc.stderr


def test_the_registry_is_required(repo: Path) -> None:
    proc = run_deploy(repo, "host", "HEAD")
    assert proc.returncode != 0
    assert "usage:" in proc.stderr
    assert not (repo / "ssh.log").exists()


def test_a_bad_sha_deploys_nothing(repo: Path) -> None:
    proc = run_deploy(repo, "host", "deadbeef", env={"MAPPD_DEPLOY_REGISTRY": "r"})
    assert proc.returncode != 0
    assert not (repo / "ssh.log").exists()
