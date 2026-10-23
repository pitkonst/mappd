"""Canary strings: one per process, carried by every holdout case, never in tracked files."""

import subprocess
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

CANARY_FILE = "canary.txt"
PREFIX = "MAPPD-CANARY-"


class Hit(BaseModel):
    """A canary found in a tracked file."""

    path: str
    line: int
    process: str


def ensure(evals_dir: Path) -> str:
    """The process's canary, created on first use."""
    path = evals_dir / CANARY_FILE
    if path.is_file():
        return path.read_text().strip()
    evals_dir.mkdir(parents=True, exist_ok=True)
    token = f"{PREFIX}{uuid4().hex}"
    path.write_text(token + "\n")
    return token


def canaries(processes_dir: Path) -> dict[str, str]:
    """Every process's canary, by process name."""
    return {
        p.parent.parent.name: p.read_text().strip()
        for p in sorted(processes_dir.glob(f"*/evals/{CANARY_FILE}"))
    }


def scan(repo: Path, tokens: dict[str, str]) -> list[Hit]:
    """Canaries in git-tracked files (binary files and canary files skipped)."""
    listed = (
        subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(repo), "ls-files", "-z"],  # noqa: S607
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .split("\0")
    )
    hits: list[Hit] = []
    for name in filter(None, listed):
        if Path(name).name == CANARY_FILE:
            continue
        try:
            text = (repo / name).read_text(encoding="utf-8")
        except UnicodeDecodeError, OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for process, token in tokens.items():
                if token in line:
                    hits.append(Hit(path=name, line=number, process=process))
    return hits
