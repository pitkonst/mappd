import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from demo_process import make_process

from mappd.process import register_process
from mappd.runner import RunError, prepare
from mappd.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(sys, "path", list(sys.path))
    return Settings(processes_dir=tmp_path / f"procs_{uuid4().hex}")


def unique() -> str:
    # Each test loads a fresh process: DBOS refuses two functions with one registered name.
    return f"p{uuid4().hex[:8]}"


def test_prepare_validates_input_against_the_process_model(settings: Settings) -> None:
    name = unique()
    make_process(settings.processes_dir, name)
    entry, case = prepare(settings, name, {"external_id": "x-1", "text": "refund now"})
    assert callable(entry)
    assert case.model_dump() == {"external_id": "x-1", "text": "refund now"}


def test_unknown_process(settings: Settings) -> None:
    with pytest.raises(RunError, match="no process 'ghost'"):
        prepare(settings, "ghost", {})


def test_invalid_input_names_the_model(settings: Settings) -> None:
    name = unique()
    make_process(settings.processes_dir, name)
    with pytest.raises(RunError, match="Input"):
        prepare(settings, name, {"text": "no id"})


def test_process_without_ingest(settings: Settings) -> None:
    root = settings.processes_dir / "bare"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text(
        "from mappd.process import Process\nprocess = Process(name='bare')\n"
    )
    register_process(settings.processes_dir, "bare")
    with pytest.raises(RunError, match="ingest"):
        prepare(settings, "bare", {})
