import sys
from pathlib import Path
from uuid import uuid4

import pytest

from mappd.data.hook import HookError, SynthSpec, load_hook


@pytest.fixture
def processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / f"procs_{uuid4().hex}"
    root.mkdir(parents=True)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return root


def test_loads_support_synth_spec() -> None:
    hook = load_hook(Path("processes"), "support")
    assert isinstance(hook, SynthSpec)
    assert hook.mix == {"reply": 0.5, "clarify": 0.2, "escalate": 0.3}
    assert hook.mix == hook.MIX
    assert hook.traps == hook.TRAPS
    assert hook.trap_rate == hook.TRAP_RATE
    assert hook.roundtrip_fields == hook.ROUNDTRIP_FIELDS
    assert hook.strict_fields == hook.STRICT_FIELDS
    assert hook.check_instructions == hook.CHECK_INSTRUCTIONS
    assert hook.INPUT_MODEL is hook.input_model
    assert hook.output_dir is not None
    assert hook.output_dir.is_dir()


def test_unknown_process_raises_hook_error(processes: Path) -> None:
    with pytest.raises(HookError, match="no process 'demo'"):
        load_hook(processes, "demo")


def test_process_without_synth_raises_hook_error(processes: Path) -> None:
    (processes / "__init__.py").write_text(
        "from mappd.process import Process\nPROCESSES = (Process(name='demo'),)\n"
    )
    with pytest.raises(HookError, match="demo has no synth"):
        load_hook(processes, "demo")
