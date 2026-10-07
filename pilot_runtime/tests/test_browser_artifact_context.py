"""0.5F-B Option C — what the BROWSER serving artifact does and does not contain.

The 0.5F-B inspection found that 70 passing local tests proved nothing about
the deployed image: the generation route failed closed with 403 there for three
independent reasons (`rung_source` never composed, the Parent package absent,
pandas absent). Source-tree tests could not see any of it, because the
deployment boundary — `deploy/gcloudignore` plus `deploy/requirements.txt` plus
the composition root — is a separate artifact no unit test exercises.

So this module reconstructs the EXACT staged build context from
`deploy/gcloudignore`, blocks the exact dependency set
`deploy/requirements.txt` omits, and runs the clinical lookup in there.

## Honest limit of this proof

There is no container runtime on the development machine, so this is an
exact-context-and-dependency proof, not a literal `docker run`. Every INPUT to
the image is accounted for — the staged file set, the installed packages, the
composition path — but the container itself is not executed here. Hosted CI
builds the literal image.

That is the same proof the inspection used, and it is what found the defect in
the first place.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "pilot_runtime" / "deploy"

#: Packages the serving `requirements.txt` deliberately does NOT install. Each
#: is blocked in the staged subprocess, so an accidental dependency on one
#: fails here rather than at the first real request.
BLOCKED_MODULES = ("pandas", "openpyxl", "xlrd", "openai", "anthropic",
                   "genex_core", "parent_taxonomy")

#: Must be IN the serving context.
REQUIRED_PATHS = (
    "pilot_runtime/data/rung_table_talking_v1.json",
    "pilot_runtime/integration/static_rung_source.py",
    "pilot_runtime/composition.py",
    "pilot_runtime/server.py",
)

#: Must NOT be in the serving context, by directory or by extension.
FORBIDDEN_DIRS = ("genex-parent", "genex_core", "parent_taxonomy", "api",
                  "notebooks")
FORBIDDEN_SUFFIXES = (".xlsx", ".xls", ".csv", ".ipynb")


@pytest.fixture(scope="module")
def staged():
    """The serving context, materialised per `deploy/gcloudignore`.

    The rules are `*` plus un-ignores for `pilot_backend/**` and
    `pilot_runtime/**`, minus tests and bytecode — so the context is exactly
    those two packages. Reproduced here by copying them with the same
    exclusions rather than by re-interpreting the glob syntax.
    """
    rules = [line.strip() for line in
             (DEPLOY / "gcloudignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    # Pin the rules this fixture is modelling. If the deploy config changes,
    # this fixture is no longer a faithful model and must be revisited.
    assert rules == ["*", "!pilot_backend/", "!pilot_backend/**",
                     "!pilot_runtime/", "!pilot_runtime/**",
                     "pilot_backend/tests/**", "pilot_runtime/tests/**",
                     "**/__pycache__/**", "**/*.pyc"], rules

    root = Path(tempfile.mkdtemp(prefix="pilot-serving-"))
    for package in ("pilot_backend", "pilot_runtime"):
        shutil.copytree(REPO_ROOT / package, root / package,
                        ignore=shutil.ignore_patterns(
                            "tests", "__pycache__", "*.pyc"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _run_in_staged(staged: Path, code: str) -> str:
    """Execute `code` inside the staged context with the blocked set absent.

    `sys.modules[name] = None` makes `import name` raise ImportError, which is
    how a missing wheel behaves. Applied BEFORE the pilot packages are
    imported, so a module-scope dependency cannot slip through.
    """
    preamble = (
        "import sys\n"
        f"sys.path.insert(0, {str(staged)!r})\n"
        f"for _blocked in {list(BLOCKED_MODULES)!r}:\n"
        "    sys.modules[_blocked] = None\n"
    )
    result = subprocess.run([sys.executable, "-c", preamble + code],
                            capture_output=True, text=True, cwd=staged)
    assert result.returncode == 0, \
        f"stdout={result.stdout}\nstderr={result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# What the context contains
# ---------------------------------------------------------------------------


def test_the_serving_context_contains_the_generated_artifact(staged):
    for relpath in REQUIRED_PATHS:
        assert (staged / relpath).is_file(), relpath
    artifact = json.loads(
        (staged / "pilot_runtime/data/rung_table_talking_v1.json")
        .read_text(encoding="utf-8"))
    # Not merely present — the SAME artifact, by digest.
    committed = json.loads(
        (REPO_ROOT / "pilot_runtime/data/rung_table_talking_v1.json")
        .read_text(encoding="utf-8"))
    assert artifact["artifact_digest"] == committed["artifact_digest"]


def test_the_serving_context_contains_no_workbook_or_parent_code(staged):
    offenders = []
    for path in staged.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(staged)
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            offenders.append(str(relative))
        if any(part in FORBIDDEN_DIRS for part in relative.parts):
            offenders.append(str(relative))
    assert offenders == []


def test_the_serving_context_holds_only_the_two_pilot_packages(staged):
    assert sorted(p.name for p in staged.iterdir()) == \
        ["pilot_backend", "pilot_runtime"]


def test_the_serving_requirements_install_nothing_forbidden():
    """The dependency half of the proof. pandas is the one that matters.

    The live `ParentGoldStandardSource` needs pandas and openpyxl to read the
    workbooks. Their absence is what makes shipping it impossible, and what
    made Option C necessary.
    """
    text = (DEPLOY / "requirements.txt").read_text()
    pins = [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]
    assert pins == ["firebase-admin==7.5.0",
                    "google-cloud-firestore==2.28.0",
                    "gunicorn==23.0.0"]
    # Checked against the PIN LINES only. The comments deliberately name
    # `openai` in order to record that it is absent, so scanning the whole file
    # would fail on the very documentation of the property being asserted.
    for blocked in BLOCKED_MODULES:
        for pin in pins:
            assert blocked not in pin.lower(), (blocked, pin)


# ---------------------------------------------------------------------------
# What the context can DO — the 18 -> 24 request, from the table alone
# ---------------------------------------------------------------------------


def test_the_staged_context_composes_a_live_rung_source(staged):
    """`rung_source != None` in the real composition, inside the real context.

    This is the exact assertion that failed before Option C.
    """
    out = _run_in_staged(staged, (
        "import json\n"
        "from pilot_runtime.composition import build_runtime\n"
        "rt = build_runtime({'PILOT_ENVIRONMENT': 'dev',\n"
        "                    'PILOT_DEV_AUTH_ENABLED': 'true'},\n"
        "                   in_memory=True)\n"
        "print(json.dumps({\n"
        "    'rung_source': type(rt.rung_source).__name__,\n"
        "    'is_none': rt.rung_source is None,\n"
        "    'app_has_same_object':\n"
        "        rt.application._rung_source is rt.rung_source,\n"
        "    'digest': rt.rung_source.artifact_digest,\n"
        "}))\n"))
    result = json.loads(out.strip())
    assert result["is_none"] is False
    assert result["rung_source"] == "StaticRungTableSource"
    assert result["app_has_same_object"] is True


def test_the_staged_context_resolves_the_pinned_eighteen_to_twenty_four(staged):
    """The whole F-B clinical algorithm, in the serving context, table only.

    Runs the real `BaselineSuggestionGenerationService.resolve_target` over a
    real `ParentBaselineProjection`: status gate -> observed floor -> static
    step lookup -> ceiling guard -> canonical rung -> mappability guard. No
    injected fixture rung and no workbook anywhere in the process.
    """
    out = _run_in_staged(staged, (
        "import json\n"
        "from pilot_runtime.composition import build_rung_source\n"
        "from pilot_backend.domain.parent_baseline_projection import (\n"
        "    ParentBaselineProjection)\n"
        "from pilot_backend.integration.baseline_suggestion_generation import (\n"
        "    BaselineSuggestionGenerationService)\n"
        "from pilot_backend.domain.entities import utc_now\n"
        "\n"
        "source = build_rung_source()\n"
        "service = BaselineSuggestionGenerationService(\n"
        "    repos=None, goals=None, rung_source=source)\n"
        "projection = ParentBaselineProjection.build(\n"
        "    child_id='chld_0123456789abcdef0123456789abcdef',\n"
        "    source_session_id='sess-fictional',\n"
        "    source_record_digest='d' * 64,\n"
        "    projection={'domain': 'talking_and_communicating',\n"
        "                'area_id': 'talking',\n"
        "                'entry_choice_id': 'many_single_words',\n"
        "                'routing_anchor_months': 18,\n"
        "                'not_demonstrated_months': 24,\n"
        "                'status': 'BOUNDED',\n"
        "                'baseline_version': "
        "'parent-2.4-functional-baseline-v1'},\n"
        "    now=utc_now())\n"
        "rung = service.resolve_target(projection)\n"
        "print(json.dumps({\n"
        "    'months': rung.source_rung_months,\n"
        "    'rung_ref': rung.rung_ref,\n"
        "    'track_ref': rung.track_ref,\n"
        "    'milestone': rung.milestone_text,\n"
        "    'subdomain': rung.subdomain,\n"
        "    'families': list(rung.activity_family_refs),\n"
        "    'mappable': rung.is_activity_mappable,\n"
        "}))\n"))
    rung = json.loads(out.strip())
    assert rung == {
        "months": 24,
        "rung_ref": "rung1:feb590cf2788978b383c11062ce67c1b",
        "track_ref": "track1:5be892494f3e6894a7de24a868084e0f",
        "milestone": "says at least two words together like more milk",
        "subdomain": "expressive_language",
        "families": ["expressive_vocabulary_growth", "two_word_phrases"],
        "mappable": True,
    }


def test_the_staged_context_still_cannot_build_the_live_adapter(staged):
    """The dependency boundary is intact, not merely unused.

    `ParentGoldStandardSource` must remain unusable in the serving context —
    that is what Option C traded away, and it should stay traded away. If this
    ever starts passing, the Parent package or pandas has leaked into the image.
    """
    out = _run_in_staged(staged, (
        "import json\n"
        "from pilot_runtime.integration.parent_gold_standard_source import (\n"
        "    ParentGoldStandardSource, default_parent_root)\n"
        "try:\n"
        "    ParentGoldStandardSource()\n"
        "    outcome = 'CONSTRUCTED'\n"
        "except Exception as exc:\n"
        "    outcome = type(exc).__name__\n"
        "print(json.dumps({'parent_root_exists': default_parent_root().exists(),\n"
        "                  'outcome': outcome}))\n"))
    result = json.loads(out.strip())
    assert result["parent_root_exists"] is False
    assert result["outcome"] == "GoldStandardSourceError"


def test_the_staged_context_imports_no_blocked_module_at_all(staged):
    """Nothing on the serving import path touches the blocked set.

    Stronger than "the route works": it proves the whole composition, including
    persistence and auth wiring, never reaches for a package the image lacks.
    """
    out = _run_in_staged(staged, (
        "import sys, json\n"
        "from pilot_runtime.composition import build_runtime\n"
        "build_runtime({'PILOT_ENVIRONMENT': 'dev',\n"
        "               'PILOT_DEV_AUTH_ENABLED': 'true'}, in_memory=True)\n"
        "print(json.dumps(sorted(\n"
        "    name for name, mod in sys.modules.items()\n"
        f"    if name.split('.')[0] in {list(BLOCKED_MODULES)!r}\n"
        "    and mod is not None)))\n"))
    assert json.loads(out.strip()) == []


# ---------------------------------------------------------------------------
# 0.6A-1 — the activity bank in the serving context
# ---------------------------------------------------------------------------


def test_the_serving_context_contains_the_activity_bank(staged):
    """It ships with no deploy-config change, like the rung table.

    Both live under `pilot_runtime/data/`, which the existing allowlist already
    admits — that is the whole reason the artifacts are placed there.
    """
    artifact = staged / "pilot_runtime/data/activity_bank_talking_v1.json"
    assert artifact.is_file()
    adapter = staged / "pilot_runtime/integration/static_activity_bank.py"
    assert adapter.is_file()
    committed = json.loads(
        (REPO_ROOT / "pilot_runtime/data/activity_bank_talking_v1.json")
        .read_text(encoding="utf-8"))
    assert json.loads(artifact.read_text(encoding="utf-8"))[
        "artifact_digest"] == committed["artifact_digest"]


def test_the_activity_bank_generator_is_not_needed_at_runtime(staged):
    """The generator ships (it is under pilot_runtime/) but is never imported.

    What matters is that the serving path does not reach it: it imports
    `genex_core`, `parent_taxonomy` and the validator, none of which exist in
    the image.
    """
    out = _run_in_staged(staged, (
        "import sys, json\n"
        "from pilot_runtime.integration.static_activity_bank import (\n"
        "    build_static_activity_bank)\n"
        "bank = build_static_activity_bank()\n"
        "print(json.dumps({\n"
        "    'generator_imported':\n"
        "        'pilot_runtime.integration.activity_bank_generator'\n"
        "        in sys.modules,\n"
        "    'release_ready': bank.release_ready,\n"
        "    'served': list(bank.served_families()),\n"
        "    'unserved': list(bank.unserved_required_families),\n"
        "    'digest': bank.artifact_digest,\n"
        "}))\n"))
    result = json.loads(out.strip())
    assert result["generator_imported"] is False
    assert result["served"] == ["book_object_naming",
                                "expressive_vocabulary_growth",
                                "sentence_building", "two_word_phrases"]
    assert result["unserved"] == []
    assert result["release_ready"] is True


def test_the_staged_bank_serves_the_pair_and_refuses_an_unserved_family(staged):
    """Both halves, proven inside the serving context rather than in-repo."""
    out = _run_in_staged(staged, (
        "import json\n"
        "from pilot_backend.integration.activity_bank import FamilyNotServed\n"
        "from pilot_runtime.integration.static_activity_bank import (\n"
        "    build_static_activity_bank)\n"
        "bank = build_static_activity_bank()\n"
        "pair = bank.templates_for_families(['expressive_vocabulary_growth',\n"
        "                                    'two_word_phrases'])\n"
        "try:\n"
        "    bank.templates_for_families(['function_question_answering'])\n"
        "    gap = 'RETURNED'\n"
        "except FamilyNotServed as exc:\n"
        "    gap = f'REFUSED: {exc}'\n"
        "print(json.dumps({'pair_count': len(pair),\n"
        "                  'families': sorted({t.activity_family_ref\n"
        "                                      for t in pair}),\n"
        "                  'unserved_family': gap}))\n"))
    result = json.loads(out.strip())
    assert result["pair_count"] == 18
    assert result["families"] == ["expressive_vocabulary_growth",
                                  "two_word_phrases"]
    assert result["unserved_family"].startswith("REFUSED")


def test_the_staged_context_has_no_validator_and_no_curated_pools(staged):
    """Validation is build-time. The image must not be able to re-run it."""
    out = _run_in_staged(staged, (
        "import importlib.util, json\n"
        "print(json.dumps({\n"
        "    'activity_validator': importlib.util.find_spec(\n"
        "        'genex_core') is not None,\n"
        "    'parent_taxonomy': importlib.util.find_spec(\n"
        "        'parent_taxonomy') is not None,\n"
        "}))\n"))
    result = json.loads(out.strip())
    assert result == {"activity_validator": False, "parent_taxonomy": False}
