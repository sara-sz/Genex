"""Repository-integrity checks (git-backed).

Assert that building the therapist service left the parent unchanged:
  * parent files under genex-parent/ have no working-tree modifications
  * genex-parent/genex_core is byte-identical to the beta-2.1-freeze tag

These skip gracefully if git is unavailable or the tag is missing.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

WORKTREE = pathlib.Path(__file__).resolve().parents[2]  # .../genex-therapist-api


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(WORKTREE),
        capture_output=True,
        text=True,
    )


def _git_available() -> bool:
    return _git("rev-parse", "--is-inside-work-tree").returncode == 0


def _tag_exists(tag: str) -> bool:
    return _git("rev-parse", "--verify", f"{tag}^{{commit}}").returncode == 0


def test_parent_files_unchanged():
    if not _git_available():
        pytest.skip("git not available")
    res = _git("status", "--porcelain", "--", "genex-parent")
    assert res.returncode == 0
    assert res.stdout.strip() == "", f"parent worktree changed:\n{res.stdout}"


def test_genex_core_byte_identical_to_freeze():
    if not _git_available():
        pytest.skip("git not available")
    if not _tag_exists("beta-2.1-freeze"):
        pytest.skip("beta-2.1-freeze tag not present")
    res = _git(
        "diff", "--name-only", "beta-2.1-freeze", "HEAD", "--", "genex-parent/genex_core"
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "", f"genex_core drifted from freeze:\n{res.stdout}"


def test_worktree_is_on_therapist_branch():
    if not _git_available():
        pytest.skip("git not available")
    res = _git("rev-parse", "--abbrev-ref", "HEAD")
    assert res.stdout.strip() == "feature/therapist-alpha-0.1"
