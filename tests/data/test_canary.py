import subprocess
from typing import TYPE_CHECKING

from mappd.data.canary import canaries, ensure, scan

if TYPE_CHECKING:
    from pathlib import Path


def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607 - fixed git argv
    return tmp_path


def track(repo: Path, *paths: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", *paths], check=True)  # noqa: S603, S607 - fixed git argv


def test_ensure_creates_once_and_canaries_finds_it(tmp_path: Path) -> None:
    evals = tmp_path / "processes" / "demo" / "evals"
    first = ensure(evals)
    assert first.startswith("MAPPD-CANARY-")
    assert ensure(evals) == first
    assert canaries(tmp_path / "processes") == {"demo": first}


def test_clean_tree_has_no_hits(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    token = ensure(repo / "processes" / "demo" / "evals")
    (repo / "prompt.md").write_text("You are helpful.\n")
    track(repo, ".")
    assert scan(repo, {"demo": token}) == []


def test_a_planted_canary_is_found(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    token = ensure(repo / "processes" / "demo" / "evals")
    (repo / "prompts").mkdir()
    (repo / "prompts" / "few_shot.md").write_text(f"Example:\n{token}\n")
    track(repo, ".")
    [hit] = scan(repo, {"demo": token})
    assert (hit.path, hit.line, hit.process) == ("prompts/few_shot.md", 2, "demo")


def test_untracked_and_binary_files_are_ignored(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    token = ensure(repo / "processes" / "demo" / "evals")
    (repo / "blob.bin").write_bytes(b"\xff\xfe" + token.encode())
    (repo / "scratch.txt").write_text(token)
    track(repo, "blob.bin", "processes")
    assert scan(repo, {"demo": token}) == []
