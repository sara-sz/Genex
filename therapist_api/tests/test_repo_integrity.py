"""Repository-integrity checks (git-backed).

Assert that building the therapist service left the protected parts of the
repository untouched:

  * BOTH genex_core copies are byte-identical to the `beta-2.1-freeze` tag
      - genex-parent/genex_core
      - genex-alpha/genex_core
  * parent files under genex-parent/ have no working-tree modifications
  * the therapist feature commits touch only therapist paths

Comparison is by git object hash at a revision (`<rev>:<path>`), which is the
recursive tree hash for a directory — so any content change anywhere beneath the
path changes the hash. A path that cannot be resolved at the freeze tag is a
FAILURE, not a pass: a guard that silently protects nothing is worse than none.
(The previous `git diff --name-only -- <path>` form returned "no differences" for
a path that did not exist at all.)

Skips are deliberately narrow: only when git itself is unavailable, or when the
freeze tag is genuinely absent from the local clone. Hosted CI checks out with
`fetch-depth: 0`, so the tag is present and these tests run rather than skip.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Dict, List, NamedTuple, Optional, Sequence

import pytest

WORKTREE = pathlib.Path(__file__).resolve().parents[2]  # .../genex-therapist-api

FREEZE_TAG = "beta-2.1-freeze"
FIRST_THERAPIST_TAG = "therapist-alpha-0.1-foundation"

# Both copies of the frozen engine. Neither may drift from the freeze tag.
GENEX_CORE_PATHS = ("genex-parent/genex_core", "genex-alpha/genex_core")

# The only paths the therapist workstream is allowed to touch.
THERAPIST_ALLOWED_PREFIXES = ("therapist_api/", ".github/workflows/therapist-api-ci.yml")


# ── git helpers ─────────────────────────────────────────────────────────────
def _git(*args: str, cwd: pathlib.Path = WORKTREE) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _git_available(cwd: pathlib.Path = WORKTREE) -> bool:
    return _git("rev-parse", "--is-inside-work-tree", cwd=cwd).returncode == 0


def _tag_exists(tag: str, cwd: pathlib.Path = WORKTREE) -> bool:
    return _git("rev-parse", "--verify", "-q", f"{tag}^{{commit}}", cwd=cwd).returncode == 0


def _object_hash(rev: str, path: str, cwd: pathlib.Path = WORKTREE) -> Optional[str]:
    """Git object hash of `path` at `rev`, or None when the path is absent there."""
    res = _git("rev-parse", f"{rev}^{{commit}}:{path}", cwd=cwd)
    return res.stdout.strip() if res.returncode == 0 else None


class Comparison(NamedTuple):
    """One protected path compared between the freeze tag and HEAD."""

    path: str
    frozen: Optional[str]
    head: Optional[str]

    @property
    def status(self) -> str:
        if self.frozen is None:
            return "missing_at_tag"    # guard would be vacuous — treat as failure
        if self.head is None:
            return "missing_at_head"   # protected path deleted
        return "unchanged" if self.frozen == self.head else "changed"


def compare_to_tag(
    paths: Sequence[str], tag: str = FREEZE_TAG, cwd: pathlib.Path = WORKTREE
) -> Dict[str, Comparison]:
    """Compare each path's object hash at `tag` against HEAD."""
    return {
        p: Comparison(p, _object_hash(tag, p, cwd), _object_hash("HEAD", p, cwd))
        for p in paths
    }


# ── skip guards (narrow, with explicit reasons) ─────────────────────────────
def _require_git(cwd: pathlib.Path = WORKTREE) -> None:
    if not _git_available(cwd):
        pytest.skip("git not available in this environment")


def _require_freeze_tag(cwd: pathlib.Path = WORKTREE) -> None:
    """Skip ONLY when the freeze tag is genuinely absent from this clone."""
    if not _tag_exists(FREEZE_TAG, cwd):
        pytest.skip(
            f"{FREEZE_TAG} tag unavailable in this clone — integrity comparison "
            "not performed. Hosted CI checks out with fetch-depth: 0 so the tag "
            "is present there; locally, fetch tags to run this check."
        )


def assert_path_frozen(result: Comparison) -> None:
    """Assert one protected path is byte-identical to the freeze tag.

    Shared by the real repository check and the simulated-drift tests, so the
    exact assertion and failure message a developer sees are the ones covered.
    """
    assert result.status != "missing_at_tag", (
        f"{result.path} does not exist at {FREEZE_TAG}; the integrity guard for "
        "this path would protect nothing. Fix the path or the reference tag."
    )
    assert result.status != "missing_at_head", f"{result.path} was deleted from HEAD"
    assert result.status == "unchanged", (
        f"genex_core drifted from {FREEZE_TAG}: {result.path}\n"
        f"  frozen: {result.frozen}\n  head:   {result.head}"
    )


def _report(request, message: str) -> None:
    """Write evidence straight to the terminal (survives pytest output capture).

    Makes the hosted run show that both paths were actually compared, rather
    than leaving a passing dot that is indistinguishable from a skip.
    """
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"[repo-integrity] {message}")


# ── the real checks against this repository ─────────────────────────────────
@pytest.mark.parametrize("path", GENEX_CORE_PATHS)
def test_genex_core_copy_byte_identical_to_freeze(path: str, request) -> None:
    """Each genex_core copy must match beta-2.1-freeze exactly."""
    _require_git()
    _require_freeze_tag()

    result = compare_to_tag([path])[path]
    _report(request, f"{path}: frozen={result.frozen} head={result.head} -> {result.status}")
    assert_path_frozen(result)


def test_both_genex_core_copies_are_checked(request) -> None:
    """Guard the guard: both copies compared, both resolvable, neither changed."""
    _require_git()
    _require_freeze_tag()

    results = compare_to_tag(GENEX_CORE_PATHS)
    assert set(results) == set(GENEX_CORE_PATHS), "a protected copy was not compared"

    for path, result in sorted(results.items()):
        _report(request, f"checked {path} -> {result.status}")
    changed = [r.path for r in results.values() if r.status != "unchanged"]
    assert not changed, f"protected paths not byte-identical to {FREEZE_TAG}: {changed}"


def test_freeze_tag_is_present_so_checks_do_not_skip() -> None:
    """In a normal clone (and in hosted CI) the comparison must actually run."""
    _require_git()
    if not _tag_exists(FREEZE_TAG):
        pytest.skip(f"{FREEZE_TAG} tag unavailable in this clone")
    assert _tag_exists(FREEZE_TAG) is True
    # ...and the guard therefore does not raise Skipped.
    _require_freeze_tag()


def test_parent_files_unchanged() -> None:
    _require_git()
    res = _git("status", "--porcelain", "--", "genex-parent")
    assert res.returncode == 0
    assert res.stdout.strip() == "", f"parent worktree changed:\n{res.stdout}"


def test_therapist_commits_touch_only_therapist_paths() -> None:
    """Scope guard: the therapist workstream must not reach outside its own tree."""
    _require_git()
    if not _tag_exists(FIRST_THERAPIST_TAG):
        pytest.skip(f"{FIRST_THERAPIST_TAG} tag unavailable in this clone")

    base = _git("rev-parse", f"{FIRST_THERAPIST_TAG}^{{commit}}^")
    assert base.returncode == 0, base.stderr
    res = _git("diff", "--name-only", base.stdout.strip(), "HEAD")
    assert res.returncode == 0, res.stderr

    touched = [f for f in res.stdout.split("\n") if f.strip()]
    assert touched, "expected the therapist commits to change at least one file"
    outside = [f for f in touched if not f.startswith(THERAPIST_ALLOWED_PREFIXES)]
    assert not outside, (
        "therapist commits modified paths outside the therapist service "
        f"(parent code, genex_core or frontend):\n  " + "\n  ".join(outside)
    )


def test_worktree_is_on_therapist_branch() -> None:
    _require_git()
    res = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = res.stdout.strip()
    # Any therapist alpha feature branch (0.1 foundation, 0.2 read-slice, ...).
    assert branch.startswith("feature/therapist-alpha-"), branch


# ── simulated-drift coverage (throwaway repos; never touches real paths) ────
def _init_repo(root: pathlib.Path) -> None:
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Integrity Test", cwd=root)
    _git("config", "commit.gpgsign", "false", cwd=root)


def _commit_all(root: pathlib.Path, message: str) -> None:
    _git("add", "-A", cwd=root)
    res = _git("commit", "-q", "-m", message, cwd=root)
    assert res.returncode == 0, res.stderr


@pytest.fixture
def frozen_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A throwaway repo holding both protected paths, tagged beta-2.1-freeze."""
    root = tmp_path / "repo"
    for path in GENEX_CORE_PATHS:
        directory = root / path
        directory.mkdir(parents=True)
        (directory / "engine.py").write_text(f"# frozen {path}\n", encoding="utf-8")
    _init_repo(root)
    _commit_all(root, "frozen baseline")
    assert _git("tag", FREEZE_TAG, cwd=root).returncode == 0
    return root


def test_simulation_unchanged_copies_pass(frozen_repo: pathlib.Path) -> None:
    results = compare_to_tag(GENEX_CORE_PATHS, cwd=frozen_repo)
    assert [r.status for r in results.values()] == ["unchanged", "unchanged"]


@pytest.mark.parametrize("drifted", GENEX_CORE_PATHS)
def test_simulation_drift_is_detected_and_names_the_path(
    frozen_repo: pathlib.Path, drifted: str
) -> None:
    """A change in either copy must fail, and must identify which copy."""
    (frozen_repo / drifted / "engine.py").write_text("# tampered\n", encoding="utf-8")
    _commit_all(frozen_repo, "simulated drift")

    results = compare_to_tag(GENEX_CORE_PATHS, cwd=frozen_repo)
    changed = [r.path for r in results.values() if r.status == "changed"]
    assert changed == [drifted], f"expected only {drifted} to be reported, got {changed}"

    # the untouched copy is still reported as unchanged (no false positive)
    other = [p for p in GENEX_CORE_PATHS if p != drifted][0]
    assert results[other].status == "unchanged"

    # The REAL assertion must fail, and its message must name the offending path.
    with pytest.raises(AssertionError) as excinfo:
        assert_path_frozen(results[drifted])
    message = str(excinfo.value)
    assert drifted in message, message
    assert FREEZE_TAG in message and "drifted" in message, message
    assert results[drifted].frozen in message and results[drifted].head in message

    # ...and the untouched copy still passes the same assertion.
    assert_path_frozen(results[other])


def test_simulation_missing_path_at_tag_is_not_a_silent_pass(
    frozen_repo: pathlib.Path,
) -> None:
    """A path absent at the freeze tag must NOT read as 'unchanged'."""
    result = compare_to_tag(["genex-parent/does_not_exist"], cwd=frozen_repo)[
        "genex-parent/does_not_exist"
    ]
    assert result.frozen is None and result.head is None
    assert result.status == "missing_at_tag"   # explicitly not "unchanged"
    with pytest.raises(AssertionError) as excinfo:
        assert_path_frozen(result)
    assert "would protect nothing" in str(excinfo.value)


def test_simulation_deleted_copy_is_detected(frozen_repo: pathlib.Path) -> None:
    target = frozen_repo / GENEX_CORE_PATHS[0]
    for child in target.iterdir():
        child.unlink()
    target.rmdir()
    _commit_all(frozen_repo, "simulated deletion")

    result = compare_to_tag(GENEX_CORE_PATHS, cwd=frozen_repo)[GENEX_CORE_PATHS[0]]
    assert result.status == "missing_at_head"
    with pytest.raises(AssertionError) as excinfo:
        assert_path_frozen(result)
    assert "was deleted from HEAD" in str(excinfo.value)


def test_simulation_missing_tag_skips_with_clear_reason(tmp_path: pathlib.Path) -> None:
    """No freeze tag -> a skip whose reason names the tag and says it is unavailable."""
    root = tmp_path / "untagged"
    root.mkdir()
    _init_repo(root)
    (root / "file.txt").write_text("x\n", encoding="utf-8")
    _commit_all(root, "no tag here")

    assert _tag_exists(FREEZE_TAG, cwd=root) is False
    with pytest.raises(pytest.skip.Exception) as excinfo:
        _require_freeze_tag(cwd=root)
    reason = str(excinfo.value)
    assert FREEZE_TAG in reason
    assert "unavailable" in reason.lower()
    assert "fetch-depth: 0" in reason   # points at why hosted CI does not skip


def test_simulation_present_tag_does_not_skip(frozen_repo: pathlib.Path) -> None:
    assert _tag_exists(FREEZE_TAG, cwd=frozen_repo) is True
    _require_freeze_tag(cwd=frozen_repo)   # must not raise Skipped
