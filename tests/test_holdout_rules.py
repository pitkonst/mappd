import subprocess
from pathlib import Path

REPO = Path(__file__).parents[1]


def ignored(path: str) -> bool:
    result = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "-C", str(REPO), "check-ignore", "-q", path],  # noqa: S607
        check=False,
    )
    return result.returncode == 0


def test_holdout_files_are_git_ignored() -> None:
    assert ignored("processes/support/evals/holdout.jsonl")
    assert ignored("processes/anything/evals/holdout-v2.jsonl")


def test_coding_agents_are_denied_the_holdout() -> None:
    for name in (".cursorignore", ".aiderignore"):
        assert "**/holdout*.jsonl" in (REPO / name).read_text().splitlines()


def test_the_claude_directory_is_git_ignored() -> None:
    assert ignored(".claude/settings.json")
    assert ignored(".claude/settings.local.json")


def test_the_holdout_sources_are_hidden_from_editors_too() -> None:
    # synth.jsonl holds every holdout case before the split; the cache holds their text.
    for name in (".cursorignore", ".aiderignore"):
        lines = (REPO / name).read_text().splitlines()
        assert {"**/evals/synth.jsonl", "**/evals/imported.jsonl", ".mappd/"} <= set(lines)
