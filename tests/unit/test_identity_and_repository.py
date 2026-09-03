from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from alpha_research_os.kernel.identity import new_experiment_id
from alpha_research_os.kernel.repository import capture_git_state


def _run(repository: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)


def test_experiment_id_format_is_deterministic_with_injected_entropy() -> None:
    identifier = new_experiment_id(
        datetime(2024, 9, 1, 12, tzinfo=UTC),
        entropy_source=lambda _: b"\x00\x01\x02",
    )

    assert identifier == "EXP-20240901-AAAQ"


def test_git_state_records_dirty_tree_and_changes_with_untracked_content(tmp_path: Path) -> None:
    _run(tmp_path, "init")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("frozen\n", encoding="utf-8")
    _run(tmp_path, "add", "tracked.txt")
    _run(
        tmp_path,
        "-c",
        "user.name=M1 Test",
        "-c",
        "user.email=m1@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    clean = capture_git_state(tmp_path)

    untracked = tmp_path / "candidate.txt"
    untracked.write_text("first\n", encoding="utf-8")
    first_dirty = capture_git_state(tmp_path)
    untracked.write_text("second\n", encoding="utf-8")
    second_dirty = capture_git_state(tmp_path)

    assert clean.is_dirty is False
    assert first_dirty.is_dirty is True
    assert "?? candidate.txt" in first_dirty.status_entries
    assert first_dirty.worktree_fingerprint != second_dirty.worktree_fingerprint
