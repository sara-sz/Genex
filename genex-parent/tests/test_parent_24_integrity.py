"""Parent 2.4 workstream integrity guards.

The Parent-side counterpart to `therapist_api/tests/test_repo_integrity.py`.
That suite is deliberately **not** reused: two of its tests
(`test_worktree_is_on_therapist_branch`,
`test_therapist_commits_touch_only_therapist_paths`) are scoped to the therapist
workstream and fail on a Parent branch *by construction*. They are correct as
written and are left completely unmodified; this file provides the equivalent
Parent-scoped protection instead.

## Only permanent invariants

Every assertion here is something that must hold for the whole Parent 2.4 era,
not merely at one checkpoint. In particular this file deliberately does **not**
pin `genex-parent/genex_core`: founder policy allows the Parent 2.4 Brain
lineage to evolve on Parent branches in later phases, so pinning it would create
a guard that a future approved phase has to weaken. `genex-alpha/genex_core` IS
pinned, because it stays frozen unless a future integration phase explicitly
says otherwise.

## Semantic over grep

Path- and object-hash comparisons are used rather than text scans. Prose is a
known false-positive source in this repository — the PARENT-0.1 audit twice
flagged "RTM" and "reply" purely because documentation *named* the things it was
promising not to build.

Skips (never failures) when git history is unavailable, so the suite stays
usable in a shallow clone or a source export. Hosted CI uses `fetch-depth: 0`,
so nothing skips there.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
from typing import List, Optional

import pytest

PARENT_ROOT = pathlib.Path(__file__).resolve().parents[1]      # genex-parent/
REPO_ROOT = PARENT_ROOT.parent                                  # worktree root

FREEZE_TAG = "beta-2.1-freeze"

#: Frozen unless a future founder-approved integration phase says otherwise.
ALPHA_CORE_PATH = "genex-alpha/genex_core"

#: The only paths the Parent 2.4 workstream may touch. `genex-parent/` includes
#: `genex_core`, which policy permits the Parent lineage to evolve later.
PARENT_ALLOWED_PREFIXES = (
    "genex-parent/",
    ".github/workflows/parent-2.4-ci.yml",
)

#: The immutable Gold Standard input snapshot (PARENT-0.2).
GOLD_STANDARD_RELPATH = (
    "data/cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx"
)
GOLD_STANDARD_SHA256 = (
    "c2b6735d9f099c916973c98c1953e7eb1f2ca8e1a60430011f805a3bf9c3487c"
)

#: Parent 2.4 begins at this frozen checkpoint.
PARENT_BASE_TAG = "parent-2.4-0.2-taxonomy-foundation"


# ── git helpers ─────────────────────────────────────────────────────────────
def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True
    )


def _git_available() -> bool:
    return _git("rev-parse", "--is-inside-work-tree").returncode == 0


def _require_git() -> None:
    if not _git_available():
        pytest.skip("not a git worktree")


def _tag_exists(tag: str) -> bool:
    return _git("rev-parse", "--verify", "-q", f"{tag}^{{commit}}").returncode == 0


def _object_hash(rev: str, path: str) -> Optional[str]:
    res = _git("rev-parse", f"{rev}^{{commit}}:{path}")
    return res.stdout.strip() if res.returncode == 0 else None


def _tags(pattern: str) -> List[str]:
    res = _git("tag", "--list", pattern)
    return [t for t in res.stdout.split("\n") if t.strip()]


# ── 1. Gold Standard provenance ─────────────────────────────────────────────
def test_original_gold_standard_is_byte_identical() -> None:
    """The 369-row source cannot be regenerated — its builder is unrecoverable."""
    path = PARENT_ROOT / GOLD_STANDARD_RELPATH
    assert path.is_file(), path
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == GOLD_STANDARD_SHA256, (
        "the immutable Gold Standard input snapshot changed; the 159->369 bridge "
        "builder is not recoverable, so this file cannot be rebuilt"
    )


def test_gold_standard_is_tracked_and_unmodified_in_git() -> None:
    _require_git()
    rel = f"genex-parent/{GOLD_STANDARD_RELPATH}"
    res = _git("status", "--porcelain=v1", "--untracked-files=all", "--", rel)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", f"Gold Standard has working-tree changes:\n{res.stdout}"


# ── 2. genex-alpha/genex_core stays frozen ──────────────────────────────────
def test_alpha_genex_core_unchanged_since_freeze() -> None:
    """Permanent: only a future approved integration phase may change this."""
    _require_git()
    if not _tag_exists(FREEZE_TAG):
        pytest.skip(f"{FREEZE_TAG} unavailable in this clone")
    frozen = _object_hash(FREEZE_TAG, ALPHA_CORE_PATH)
    head = _object_hash("HEAD", ALPHA_CORE_PATH)
    assert frozen is not None, "guard would be vacuous: path missing at the freeze tag"
    assert head is not None, f"{ALPHA_CORE_PATH} was deleted"
    assert frozen == head, (
        f"{ALPHA_CORE_PATH} drifted from {FREEZE_TAG}; it is frozen unless a "
        "founder-approved integration phase says otherwise"
    )


# ── 3. Historical therapist checkpoints are immutable ───────────────────────
def test_therapist_tags_still_resolve_to_their_frozen_commits() -> None:
    """Parent work must never move or rewrite a therapist checkpoint."""
    _require_git()
    tags = _tags("therapist-alpha-*")
    if not tags:
        pytest.skip("therapist-alpha tags unavailable in this clone")
    assert len(tags) >= 19, f"expected at least 19 therapist tags, found {len(tags)}"
    for tag in tags:
        res = _git("rev-parse", f"{tag}^{{commit}}")
        assert res.returncode == 0, f"{tag} does not resolve: {res.stderr}"
        assert res.stdout.strip(), tag


def test_parent_work_did_not_delete_any_therapist_tag() -> None:
    _require_git()
    if not _tag_exists("therapist-alpha-0.7.4-private-note-write"):
        pytest.skip("therapist tags unavailable in this clone")
    res = _git("rev-parse", "therapist-alpha-0.7.4-private-note-write^{commit}")
    assert res.stdout.strip() == "7d1257b60aba5d150e9595da92a9b7d0b65e5614"


# ── 4. Parent workstream scope ──────────────────────────────────────────────
def test_parent_commits_touch_only_parent_paths() -> None:
    """Scope guard: Parent 2.4 work must not reach outside the Parent tree.

    The Parent mirror of the therapist scope guard. `genex-parent/` is allowed
    in full (including `genex_core`, which the Parent 2.4 lineage may evolve
    later); `therapist_api/`, `genex-alpha/`, `webapp/` and deployment config
    are not.
    """
    _require_git()
    if not _tag_exists(PARENT_BASE_TAG):
        pytest.skip(f"{PARENT_BASE_TAG} unavailable in this clone")

    base = _git("rev-parse", f"{PARENT_BASE_TAG}^{{commit}}")
    assert base.returncode == 0, base.stderr
    res = _git("diff", "--name-only", base.stdout.strip(), "HEAD")
    assert res.returncode == 0, res.stderr

    touched = [f for f in res.stdout.split("\n") if f.strip()]
    outside = [f for f in touched if not f.startswith(PARENT_ALLOWED_PREFIXES)]
    assert not outside, (
        "Parent 2.4 commits modified paths outside the Parent workstream "
        "(therapist service, genex-alpha, frontend or deploy config):\n  "
        + "\n  ".join(outside)
    )


def test_parent_work_did_not_touch_the_therapist_service() -> None:
    _require_git()
    if not _tag_exists(PARENT_BASE_TAG):
        pytest.skip(f"{PARENT_BASE_TAG} unavailable in this clone")
    base = _git("rev-parse", f"{PARENT_BASE_TAG}^{{commit}}").stdout.strip()
    res = _git("diff", "--name-only", base, "HEAD", "--", "therapist_api")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", f"therapist_api changed:\n{res.stdout}"


def test_therapist_workflow_is_unmodified() -> None:
    """The historical therapist gate must keep running exactly as frozen."""
    _require_git()
    if not _tag_exists(PARENT_BASE_TAG):
        pytest.skip(f"{PARENT_BASE_TAG} unavailable in this clone")
    base = _git("rev-parse", f"{PARENT_BASE_TAG}^{{commit}}").stdout.strip()
    res = _git(
        "diff", "--name-only", base, "HEAD",
        "--", ".github/workflows/therapist-api-ci.yml",
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", "the therapist workflow was modified"


# ── 5. No RTM / no production surface introduced ────────────────────────────
def test_no_rtm_module_introduced_in_parent_tree() -> None:
    """Structural, not textual: prose legitimately NAMES RTM to exclude it."""
    banned = ("rtm", "cpt", "billing", "payer", "attestation", "claim")
    offenders = []
    for path in sorted(PARENT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        stem = path.stem.lower()
        if any(b in stem for b in banned):
            offenders.append(str(path.relative_to(PARENT_ROOT)))
    assert offenders == [], f"RTM-shaped modules introduced: {offenders}"


def test_parent_taxonomy_package_has_no_third_party_hard_dependency() -> None:
    """domains/subdomain_map must stay importable with stdlib alone.

    Keeps the canonical vocabulary usable from any consumer — including ones
    that must not drag pandas in. `migrate` may use pandas, but only inside
    functions.
    """
    import ast

    pkg = PARENT_ROOT / "parent_taxonomy"
    for name in ("domains.py", "subdomain_map.py"):
        tree = ast.parse((pkg / name).read_text())
        for node in ast.walk(tree):
            mod = ""
            if isinstance(node, ast.Import):
                mod = node.names[0].name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            root = mod.split(".")[0]
            assert root not in ("pandas", "numpy", "openpyxl"), f"{name} imports {mod}"


def test_migrate_keeps_pandas_imports_function_local() -> None:
    import ast

    tree = ast.parse((PARENT_ROOT / "parent_taxonomy" / "migrate.py").read_text())
    for node in tree.body:  # module level only
        if isinstance(node, ast.Import):
            assert node.names[0].name.split(".")[0] != "pandas"
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "pandas"
