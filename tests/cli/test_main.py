import subprocess
import sys

from typer.testing import CliRunner

from mappd import __version__
from mappd.cli import app

runner = CliRunner()


def test_version() -> None:
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert r.stdout.strip() == __version__


def test_no_args_prints_help() -> None:
    r = runner.invoke(app, [])
    assert "doctor" in r.stdout


def test_python_dash_m_runs_the_cli() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "mappd", "--version"], capture_output=True, text=True, check=True
    )
    assert r.stdout.strip() == __version__
