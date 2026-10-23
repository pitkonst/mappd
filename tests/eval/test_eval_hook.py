import sys
from pathlib import Path
from uuid import uuid4

import pytest

from mappd.eval.hook import EvalHookError, EvalSpec, load_eval_hook


def test_missing_process_is_named(tmp_path: Path) -> None:
    with pytest.raises(EvalHookError, match="no process 'proc'"):
        load_eval_hook(tmp_path, "proc")


def test_process_without_evals_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    pkg = tmp_path / f"evalhook_{uuid4().hex}"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from mappd.process import Process\nPROCESSES = (Process(name='half'),)\n"
    )
    with pytest.raises(EvalHookError, match="half has no evals"):
        load_eval_hook(pkg, "half")


def test_support_eval_spec_loads_with_explicit_paths() -> None:
    hook = load_eval_hook(Path("processes"), "support")
    assert isinstance(hook, EvalSpec)
    assert "keywords" in hook.baselines
    assert hook.baselines == hook.BASELINES
    assert hook.config_path is not None
    assert hook.config_path.is_file()
    assert hook.rubric_path is not None
    assert hook.rubric_path.is_file()
    assert hook.dev_path is not None
    assert hook.dev_path.is_file()
    assert hook.holdout_path is not None
    assert hook.canary_path is not None
    assert hook.canary_path.is_file()
