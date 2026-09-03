"""Capture a reproducible Git worktree identity, including dirty content."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .canonical import sha256_bytes
from .specs import GitStateSpec


def _git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return result.stdout


def _untracked_fingerprint(repository: Path, paths: tuple[str, ...]) -> bytes:
    digest = hashlib.sha256()
    for relative in sorted(paths):
        path = repository / relative
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        elif path.is_symlink():
            digest.update(str(path.readlink()).encode("utf-8"))
        digest.update(b"\0")
    return digest.digest()


def capture_git_state(repository: Path) -> GitStateSpec:
    """Capture HEAD plus tracked and untracked dirty content fingerprints."""

    root = repository.resolve(strict=True)
    commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    status_raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = tuple(item for item in status_raw.decode("utf-8", errors="surrogateescape").split("\0") if item)
    diff = _git(root, "diff", "--binary", "HEAD", "--")
    untracked = tuple(entry[3:] for entry in entries if entry.startswith("?? "))
    fingerprint_payload = b"status\0" + status_raw + b"diff\0" + diff
    fingerprint_payload += b"untracked\0" + _untracked_fingerprint(root, untracked)
    return GitStateSpec(
        commit=commit,
        is_dirty=bool(entries),
        status_entries=entries,
        worktree_fingerprint=sha256_bytes(fingerprint_payload),
    )
