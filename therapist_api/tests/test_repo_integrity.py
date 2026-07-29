"""Repository-integrity checks (git-backed).

Assert that building the therapist service left the protected parts of the
repository untouched:

  * BOTH genex_core copies are byte-identical to the `beta-2.1-freeze` tag
      - genex-parent/genex_core
      - genex-alpha/genex_core
  * parent files under genex-parent/ have no genuine working-tree modifications
  * the therapist feature commits touch only therapist paths

Two DISTINCT protections, deliberately kept separate:

  A. Committed history — `test_therapist_commits_touch_only_therapist_paths`
     proves no therapist commit altered a Parent path. This is the guarantee
     hosted CI actually needs.
  B. Local working tree — `test_parent_files_unchanged` catches a developer
     editing Parent files while working on the therapist service.

(B) must tolerate git *filter* artifacts. Several Parent files match an
`*.xlsx filter=lfs` rule in .gitattributes but were committed as raw bytes on a
machine without git-lfs, so their blobs are real content rather than pointers.
Wherever git-lfs IS active (e.g. GitHub runners) `git status` pipes them through
the LFS clean filter, gets a pointer, and reports "modified" although nothing was
written. See `_is_filter_artifact` for how that is distinguished from a real edit.

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
import sys
from typing import Dict, List, NamedTuple, Optional, Sequence

import pytest

WORKTREE = pathlib.Path(__file__).resolve().parents[2]  # .../genex-therapist-api

FREEZE_TAG = "beta-2.1-freeze"
FIRST_THERAPIST_TAG = "therapist-alpha-0.1-foundation"

# Both copies of the frozen engine. Neither may drift from the freeze tag.
GENEX_CORE_PATHS = ("genex-parent/genex_core", "genex-alpha/genex_core")

# The only paths the therapist workstream is allowed to touch.
THERAPIST_ALLOWED_PREFIXES = ("therapist_api/", ".github/workflows/therapist-api-ci.yml")

# Working-tree protection scope.
PARENT_PATH = "genex-parent"

# Status codes that MAY turn out to be a filter round-trip rather than an edit.
# Only plain modification qualifies; add/delete/rename/copy/typechange/unmerged
# and untracked are always genuine.
FILTER_ARTIFACT_CODES = frozenset("M ")


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


# ── working-tree status parsing (NUL-delimited, filter-aware) ───────────────
class MalformedStatus(RuntimeError):
    """git status output could not be parsed — fail closed, never ignore."""


class StatusEntry(NamedTuple):
    """One `git status --porcelain=v1 -z` record."""

    x: str                        # staged (index vs HEAD) code
    y: str                        # unstaged (worktree vs index) code
    path: str                     # for a rename/copy this is the NEW path
    orig_path: Optional[str]      # rename/copy source, else None

    @property
    def code(self) -> str:
        return f"{self.x}{self.y}"


def parse_porcelain_z(output: str) -> List[StatusEntry]:
    """Parse `git status --porcelain=v1 -z` output.

    The -z format is used precisely because it is unambiguous: paths are NEVER
    quoted or backslash-escaped, so names containing spaces, quotes or newlines
    survive intact. Each record is `XY <path>` terminated by NUL. A rename/copy
    record is followed by a SECOND NUL-terminated field holding the ORIGINAL
    path — the new path comes first (verified directly against git).

    Anything that does not match this shape raises MalformedStatus rather than
    being skipped.
    """
    fields = output.split("\0")
    if fields and fields[-1] == "":
        fields.pop()              # trailing empty field after the final NUL

    entries: List[StatusEntry] = []
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if len(record) < 4 or record[2] != " ":
            raise MalformedStatus(f"unparseable git status record: {record!r}")
        x, y, path = record[0], record[1], record[3:]
        orig_path = None
        if "R" in (x, y) or "C" in (x, y):
            if i >= len(fields):
                raise MalformedStatus(f"rename/copy record missing source path: {record!r}")
            orig_path = fields[i]
            i += 1
        entries.append(StatusEntry(x, y, path, orig_path))
    return entries


def _is_filter_artifact(entry: StatusEntry, cwd: pathlib.Path) -> bool:
    """True only when the file's REAL bytes and its index entry both match HEAD.

    `git status` compares the working tree through any configured clean filter,
    so an LFS-declared file stored as raw bytes reads as modified on a machine
    with git-lfs active even though nothing was written. `git hash-object
    --no-filters` hashes the bytes actually on disk, bypassing that.

    Both conditions are required. Checking the index too means a genuinely
    STAGED change is never mistaken for an artifact, even if the working-tree
    bytes happen to match HEAD.
    """
    head = _git("rev-parse", f"HEAD:{entry.path}", cwd=cwd)
    if head.returncode != 0:
        return False                          # not in HEAD -> cannot be a round-trip
    head_blob = head.stdout.strip()

    index = _git("ls-files", "-s", "--", entry.path, cwd=cwd)
    index_fields = index.stdout.split()
    if index.returncode != 0 or len(index_fields) < 2 or index_fields[1] != head_blob:
        return False                          # staged difference -> genuine

    raw = _git("hash-object", "--no-filters", "--", entry.path, cwd=cwd)
    if raw.returncode != 0:
        return False
    return raw.stdout.strip() == head_blob


def classify_status_entry(entry: StatusEntry, cwd: pathlib.Path) -> Optional[str]:
    """Reason this entry is a genuine Parent change, or None if benign.

    Fail-closed: every code that is not provably a filter round-trip counts as a
    real change, and an unrecognised code is reported rather than ignored.
    """
    code = entry.code
    if entry.orig_path is not None:
        return "renamed/copied parent path"          # fail closed, never excused
    if "?" in code:
        return "untracked parent file"
    if "D" in code:
        return "deleted parent file"
    if "A" in code:
        return "added parent file"
    if "T" in code:
        return "parent file type changed"
    if "U" in code:
        return "unmerged parent file"
    if set(code) <= FILTER_ARTIFACT_CODES:
        if not code.strip():
            raise MalformedStatus(f"empty status code for {entry.path!r}")
        if _is_filter_artifact(entry, cwd):
            return None                              # LFS/filter round-trip only
        return "parent file content differs from HEAD"
    raise MalformedStatus(f"unrecognised git status code {code!r} for {entry.path!r}")


def genuine_parent_changes(cwd: pathlib.Path = WORKTREE) -> List[tuple]:
    """(entry, reason) for every REAL change under genex-parent/."""
    res = _git(
        "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", PARENT_PATH, cwd=cwd
    )
    if res.returncode != 0:
        raise MalformedStatus(f"git status failed: {res.stderr.strip()}")
    return [
        (entry, reason)
        for entry in parse_porcelain_z(res.stdout)
        for reason in [classify_status_entry(entry, cwd)]
        if reason is not None
    ]


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


def test_parent_files_unchanged(request) -> None:
    """LOCAL WORKING-TREE protection: no genuine edit to Parent files.

    This is protection (B) from the module docstring. It is NOT the proof that
    therapist commits left Parent code alone — that is
    `test_therapist_commits_touch_only_therapist_paths`, which inspects
    committed history and is the guarantee hosted CI relies on.

    A `git status` entry is ignored ONLY when the raw on-disk bytes and the
    index entry both still equal the committed HEAD blob, i.e. the entry is a
    git filter round-trip (LFS-declared files stored as raw bytes) and nothing
    was actually written. Genuine edits, staged changes, additions, deletions,
    renames and copies all still fail, and an unparseable or unrecognised status
    raises rather than passing quietly.

    What this test sees differs legitimately by environment:

      * On a developer machine WITHOUT git-lfs, no filter runs and git usually
        reports a clean status — the exemption is never needed.
      * On GitHub's checkout, git-lfs IS installed, so LFS-declared Parent files
        stored as raw bytes may arrive as filter-induced status entries.

    In BOTH environments a genuine byte or index change fails. Hosted run #5
    exercised the real GitHub LFS checkout and this helper classified it as
    `parent worktree: 0 genuine change(s)` — the production path is verified
    against a real LFS checkout, not only against simulations.

    No filename is hard-coded: the helper classifies whatever git reports under
    `genex-parent/`, so it stays correct if the set of LFS-declared Parent files
    changes.
    """
    _require_git()
    changes = genuine_parent_changes()
    _report(request, f"parent worktree: {len(changes)} genuine change(s)")
    assert not changes, "parent worktree changed:\n" + "\n".join(
        f"  {entry.code!r} {entry.path} — {reason}" for entry, reason in changes
    )


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


# ── parent working-tree protection (throwaway repos; stand-in files only) ───
SPACED_NAME = "genex-parent/data/cdc milestones copy.xlsx"


@pytest.fixture
def parent_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Throwaway repo with Parent-like files, one declared LFS but stored raw.

    Mirrors the real repository's condition: `.gitattributes` marks *.xlsx as
    LFS-managed, but the blobs are committed as raw bytes because no clean
    filter is configured at commit time.
    """
    root = tmp_path / "parent-repo"
    (root / "genex-parent" / "data").mkdir(parents=True)
    (root / ".gitattributes").write_text("*.xlsx filter=lfs diff=lfs merge=lfs -text\n",
                                         encoding="utf-8")
    (root / "genex-parent" / "app.py").write_text("# parent code\n", encoding="utf-8")
    # Stand-in binary content — never the real protected spreadsheet.
    (root / "genex-parent" / "data" / "sheet.xlsx").write_bytes(b"PK\x03\x04stand-in" * 64)
    (root / SPACED_NAME).write_bytes(b"PK\x03\x04spaced" * 32)
    _init_repo(root)
    _commit_all(root, "parent baseline")
    return root


LFS_TRACKED = "genex-parent/data/sheet.xlsx"


# A filter-induced status record, exactly as `git status --porcelain=v1 -z`
# emits it: unstaged modification, NUL-terminated, path unquoted.
SYNTHETIC_FILTER_STATUS = f" M {LFS_TRACKED}\0"


def _stub_status_only(monkeypatch, record: str) -> None:
    """Make ONLY the scoped `git status` call return `record`; all else is real.

    Provoking a real filter-induced status is not portable. git-lfs installs
    `filter.lfs.process` globally (that is what `git lfs install` does), and a
    `process` filter TAKES PRECEDENCE over `filter.lfs.clean` — so a repo-local
    clean filter is simply never invoked on a machine that has git-lfs, such as
    a GitHub runner. Rather than fight that, the status output is injected and
    everything the classifier actually reasons about stays real:

        git rev-parse HEAD:<path>      -> real committed blob
        git ls-files -s -- <path>      -> real index blob
        git hash-object --no-filters   -> real bytes on disk

    So the production decision path is exercised end to end; only the trigger
    is synthetic.
    """
    real_git = _git
    module = sys.modules[__name__]

    def fake_git(*args: str, cwd: pathlib.Path = WORKTREE) -> subprocess.CompletedProcess:
        if args and args[0] == "status":
            return subprocess.CompletedProcess(list(args), 0, record, "")
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(module, "_git", fake_git)


def test_parent_sim_clean_tree_passes(parent_repo: pathlib.Path) -> None:
    assert genuine_parent_changes(cwd=parent_repo) == []


def _real_hashes(repo: pathlib.Path, rel: str) -> tuple:
    """(HEAD blob, index blob, raw on-disk hash) — all from real git, no stubbing."""
    head = _git("rev-parse", f"HEAD:{rel}", cwd=repo).stdout.strip()
    index_fields = _git("ls-files", "-s", "--", rel, cwd=repo).stdout.split()
    raw = _git("hash-object", "--no-filters", "--", rel, cwd=repo).stdout.strip()
    return head, (index_fields[1] if len(index_fields) > 1 else ""), raw


def test_parent_sim_lfs_filter_artifact_passes(parent_repo: pathlib.Path, monkeypatch) -> None:
    """A filter-only status entry is ignored — the real hashes decide."""
    _stub_status_only(monkeypatch, SYNTHETIC_FILTER_STATUS)

    # The status record the classifier will see is the real porcelain shape.
    reported = parse_porcelain_z(SYNTHETIC_FILTER_STATUS)
    assert [(e.code, e.path, e.orig_path) for e in reported] == [(" M", LFS_TRACKED, None)]

    # Nothing was actually written: committed blob, index and disk all agree.
    head, index, raw = _real_hashes(parent_repo, LFS_TRACKED)
    assert head and head == index == raw

    assert genuine_parent_changes(cwd=parent_repo) == []


def test_parent_sim_real_modification_fails(parent_repo: pathlib.Path) -> None:
    (parent_repo / "genex-parent" / "app.py").write_text("# edited\n", encoding="utf-8")
    changes = genuine_parent_changes(cwd=parent_repo)
    assert [e.path for e, _ in changes] == ["genex-parent/app.py"]
    assert changes[0][1] == "parent file content differs from HEAD"


def test_parent_sim_real_modification_fails_even_with_filter_active(
    parent_repo: pathlib.Path, monkeypatch
) -> None:
    """The artifact exemption must not mask a genuine edit to an LFS file.

    The SAME synthetic status record is used throughout — only the bytes on disk
    change — so the exemption is the sole thing under test.
    """
    _stub_status_only(monkeypatch, SYNTHETIC_FILTER_STATUS)
    assert genuine_parent_changes(cwd=parent_repo) == []       # artifact only so far

    target = parent_repo / LFS_TRACKED
    target.write_bytes(target.read_bytes() + b"genuinely changed")

    head, index, raw = _real_hashes(parent_repo, LFS_TRACKED)
    assert head == index and raw != head        # bytes really differ now

    changes = genuine_parent_changes(cwd=parent_repo)
    assert [e.path for e, _ in changes] == [LFS_TRACKED]
    assert changes[0][1] == "parent file content differs from HEAD"


def test_parent_sim_staged_modification_fails(parent_repo: pathlib.Path) -> None:
    (parent_repo / "genex-parent" / "app.py").write_text("# staged edit\n", encoding="utf-8")
    _git("add", "genex-parent/app.py", cwd=parent_repo)
    changes = genuine_parent_changes(cwd=parent_repo)
    assert [e.path for e, _ in changes] == ["genex-parent/app.py"]
    assert changes[0][0].x == "M"   # staged


def test_parent_sim_staged_edit_reverted_on_disk_still_fails(
    parent_repo: pathlib.Path,
) -> None:
    """Index differs from HEAD even though on-disk bytes match — still genuine."""
    original = (parent_repo / "genex-parent" / "app.py").read_text(encoding="utf-8")
    (parent_repo / "genex-parent" / "app.py").write_text("# staged\n", encoding="utf-8")
    _git("add", "genex-parent/app.py", cwd=parent_repo)
    (parent_repo / "genex-parent" / "app.py").write_text(original, encoding="utf-8")

    changes = genuine_parent_changes(cwd=parent_repo)
    assert [e.path for e, _ in changes] == ["genex-parent/app.py"]


def test_parent_sim_deleted_file_fails(parent_repo: pathlib.Path) -> None:
    (parent_repo / "genex-parent" / "app.py").unlink()
    changes = genuine_parent_changes(cwd=parent_repo)
    assert [(e.path, r) for e, r in changes] == [("genex-parent/app.py", "deleted parent file")]


def test_parent_sim_untracked_file_fails(parent_repo: pathlib.Path) -> None:
    (parent_repo / "genex-parent" / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    changes = genuine_parent_changes(cwd=parent_repo)
    assert [(e.path, r) for e, r in changes] == [
        ("genex-parent/new_module.py", "untracked parent file")
    ]


def test_parent_sim_rename_fails(parent_repo: pathlib.Path) -> None:
    _git("mv", "genex-parent/app.py", "genex-parent/app_renamed.py", cwd=parent_repo)
    changes = genuine_parent_changes(cwd=parent_repo)
    assert changes, "a rename must be reported"
    entry, reason = changes[0]
    assert reason == "renamed/copied parent path"
    # -z puts the NEW path first and the ORIGINAL in the following field.
    assert entry.path == "genex-parent/app_renamed.py"
    assert entry.orig_path == "genex-parent/app.py"


def test_parent_sim_path_with_spaces_is_parsed(parent_repo: pathlib.Path) -> None:
    target = parent_repo / SPACED_NAME
    target.write_bytes(target.read_bytes() + b"edited")
    changes = genuine_parent_changes(cwd=parent_repo)
    assert [e.path for e, _ in changes] == [SPACED_NAME]   # unsplit, unquoted


def test_parent_sim_failure_message_names_the_path(parent_repo: pathlib.Path) -> None:
    (parent_repo / "genex-parent" / "app.py").write_text("# edited\n", encoding="utf-8")
    changes = genuine_parent_changes(cwd=parent_repo)
    message = "parent worktree changed:\n" + "\n".join(
        f"  {e.code!r} {e.path} — {r}" for e, r in changes
    )
    assert "genex-parent/app.py" in message
    assert "differs from HEAD" in message


def test_parse_porcelain_z_handles_every_record_shape() -> None:
    raw = (
        " M genex-parent/plain.txt\0"
        "R  genex-parent/new name.txt\0genex-parent/old name.txt\0"
        " D genex-parent/gone.txt\0"
        "?? genex-parent/untracked file.txt\0"
    )
    entries = parse_porcelain_z(raw)
    assert [(e.code, e.path, e.orig_path) for e in entries] == [
        (" M", "genex-parent/plain.txt", None),
        ("R ", "genex-parent/new name.txt", "genex-parent/old name.txt"),
        (" D", "genex-parent/gone.txt", None),
        ("??", "genex-parent/untracked file.txt", None),
    ]
    assert parse_porcelain_z("") == []

    # The exact record shape a filter round-trip produces.
    single = parse_porcelain_z(" M genex-parent/data/sheet.xlsx\0")
    assert [(e.x, e.y, e.path, e.orig_path) for e in single] == [
        (" ", "M", "genex-parent/data/sheet.xlsx", None)
    ]


def test_malformed_status_fails_closed(parent_repo: pathlib.Path) -> None:
    """Unparseable or unrecognised status must raise, never pass quietly."""
    with pytest.raises(MalformedStatus):
        parse_porcelain_z("garbage-without-status-field\0")
    with pytest.raises(MalformedStatus):   # rename record missing its source path
        parse_porcelain_z("R  genex-parent/only-one-field.txt\0")
    with pytest.raises(MalformedStatus):   # unrecognised code
        classify_status_entry(StatusEntry("X", "Z", "genex-parent/app.py", None), parent_repo)
