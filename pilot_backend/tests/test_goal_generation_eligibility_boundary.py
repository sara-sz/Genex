"""A2 v2.1 — profile information cannot influence target selection.

`GoalService.generate_anchored_suggestion` is the shared goal-generation
chokepoint: the only method that writes a canonical anchor, and the only path
by which a `GoalSuggestion` comes into existence from baseline evidence.

The founder rule this file proves STRUCTURALLY rather than behaviourally:

    projection into the Pilot != permission to generate a GoalSuggestion

and, specifically, that the chokepoint cannot consume

    * raw Channel-B probes
    * profile concern state
    * multi-area state
    * not-assessed reasons

to select or replace a target.

## WHY THIS IS A STRUCTURAL ARGUMENT AND NOT A LIST OF CASES

The chokepoint does not SELECT a target. It receives one, as `canonical_rung`,
already resolved by the caller from Channel-A band evidence. So the question
"could profile state change which target is generated" reduces to "can profile
state reach any input of this method" — which is a question about types, not
about data, and therefore answerable exhaustively.

Its inputs are `principal`, `child_id`, `projection`, `canonical_rung`,
`observed`, `policy`, `generation_policy`, `request_id`. The A2 v2.1 clinician
profile is deliberately stored as its OWN record rather than as a field on
`ParentBaselineProjectionV2`, so none of those inputs has anywhere to carry it.
Had the profile been added to the projection — the obvious implementation — this
proof would be impossible to state.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

from pilot_backend.domain import parent_baseline_projection_v2 as proj_v2
from pilot_backend.domain.canonical_rung import CanonicalRung
from pilot_backend.goals.service import GoalService
from pilot_backend.integration import baseline_suggestion_generation_v2 as gen2

#: Every Parent-side profile concept, by the name it has on the Parent side.
#: If any of these ever appears in the generation path, the separation has been
#: lost.
PROFILE_CONCEPTS = (
    "profile_probes", "profile_area", "profile_block", "profile_summary",
    "concern_observed", "areas_with_concern", "concerning_areas",
    "multi_area", "multi_area_indeterminate",
    "not_assessed", "not_assessed_cause", "not_assessed_budget_consumed",
    "not_assessed_no_age_appropriate_skill",
    "uncertain_areas", "areas_uncertain", "recoverable_areas",
    "INDETERMINATE", "NOT_OBSERVED",
)

GENERATION_PATH = (
    "pilot_backend/goals/service.py",
    "pilot_backend/goals/suggestion_engine.py",
    "pilot_backend/integration/baseline_suggestion_generation_v2.py",
    "pilot_backend/domain/parent_baseline_projection_v2.py",
    "pilot_backend/domain/canonical_rung.py",
    "pilot_backend/domain/goals.py",
)


def _repo_root():
    return pathlib.Path(__file__).resolve().parents[2]


def _code_without_docstrings(source: str) -> str:
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# 1. the chokepoint's inputs cannot carry profile state
# ---------------------------------------------------------------------------

def test_the_chokepoint_signature_is_exactly_these_inputs():
    """Pinned, so a new parameter is a visible, reviewed event."""
    params = list(inspect.signature(
        GoalService.generate_anchored_suggestion).parameters)
    assert params == [
        "self", "principal", "child_id", "projection", "canonical_rung",
        "observed", "policy", "generation_policy",
        "parent_content_evaluation", "request_id",
    ], params
    # `parent_content_evaluation` is ADVISORY provenance, never authority —
    # see `_require_current_content_support`. It can refuse a generation; it
    # can never authorise one, and it can never name a different target.
    assert "parent_content_evaluation" in params


def test_the_chokepoint_receives_its_target_and_does_not_select_one():
    """The target is a PARAMETER. Selection happens upstream, from Channel A.

    So nothing inside this method can choose a different target, whatever it
    can see — which is the first half of "cannot select or replace".
    """
    code = _code_without_docstrings(
        inspect.getsource(GoalService.generate_anchored_suggestion))
    # It never reassigns the target, and never reaches for another rung.
    assert not re.search(r"\bcanonical_rung\s*=", code)
    for forbidden in ("resolve_targets", "band_views", "declared_band_roster",
                      "next_rung", "_step", "nearest"):
        assert forbidden not in code, forbidden


def test_the_projection_record_has_no_profile_field():
    """THE LOAD-BEARING SEPARATION.

    The A2 v2.1 clinician profile is its own record. Adding it to the
    projection — the obvious implementation — would have handed the generator a
    legitimate reference to profile state.
    """
    fields = set(proj_v2.ParentBaselineProjectionV2.__dataclass_fields__)
    for concept in PROFILE_CONCEPTS:
        assert concept not in fields, concept
    # And no field is even named suggestively.
    for field in fields:
        lowered = field.lower()
        for fragment in ("profile", "concern", "area_state", "multi"):
            assert fragment not in lowered, (field, fragment)


def test_the_canonical_rung_carries_no_profile_state():
    fields = set(CanonicalRung.__dataclass_fields__)
    for concept in PROFILE_CONCEPTS:
        assert concept not in fields, concept


# ---------------------------------------------------------------------------
# 2. no profile concept exists anywhere on the generation path
# ---------------------------------------------------------------------------

def test_no_profile_concept_appears_on_the_generation_path():
    """Scanned over executable code, docstrings stripped.

    These modules are the complete path from a stored projection to a written
    `GoalSuggestion`. A profile concept appearing in any of them is the
    separation failing, whether or not a test happens to exercise it.
    """
    root = _repo_root()
    offenders = {}
    for relative in GENERATION_PATH:
        path = root / relative
        assert path.exists(), relative
        code = _code_without_docstrings(path.read_text(encoding="utf-8"))
        found = [c for c in PROFILE_CONCEPTS if c in code]
        if found:
            offenders[relative] = found
    assert offenders == {}, offenders


def test_the_generation_path_does_not_import_the_parent_api():
    """Parent derives the profile; the Pilot must not reach back for it."""
    root = _repo_root()
    for relative in GENERATION_PATH:
        source = (root / relative).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(a.name for a in node.names)
            if module:
                assert "genex_core" not in module, (relative, module)
                assert not module.startswith("api"), (relative, module)
                assert "baseline_profile" not in module, (relative, module)
                assert "baseline_contract" not in module, (relative, module)


#: The ONLY modules allowed to know the profile vocabulary: the v2.1 record
#: itself and its codec. Everything else — above all the generation path — must
#: not, and `test_no_profile_concept_appears_on_the_generation_path` keeps that
#: true independently of this allowlist.
PROFILE_AWARE_MODULES = frozenset({
    "domain/parent_clinician_profile_v21.py",
    "transport/projection_v21_request.py",
})


def test_only_the_v21_record_and_its_codec_know_the_profile_vocabulary():
    """The A2 v2.1 containment rule.

    This started as "zero occurrences anywhere", recorded before the v2.1 work
    so the record landing would have to come through here. It now names the
    two modules that may know these words, which is the same guarantee stated
    once the record exists: a third module acquiring them is a reviewed event.
    """
    root = _repo_root() / "pilot_backend"
    hits = {}
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        relative = str(path.relative_to(root))
        code = _code_without_docstrings(path.read_text(encoding="utf-8"))
        found = [c for c in ("profile_probes", "multi_area",
                             "concern_observed") if c in code]
        if found and relative not in PROFILE_AWARE_MODULES:
            hits[relative] = found
    assert hits == {}, hits


def test_the_profile_aware_modules_really_do_carry_the_vocabulary():
    """Non-vacuity: the allowlist describes reality, not an aspiration."""
    root = _repo_root() / "pilot_backend"
    for relative in PROFILE_AWARE_MODULES:
        code = _code_without_docstrings(
            (root / relative).read_text(encoding="utf-8"))
        assert "multi_area" in code, relative


def test_no_profile_aware_module_is_on_the_generation_path():
    """The two sets must stay disjoint, by construction."""
    generation = {p.split("pilot_backend/", 1)[1] for p in GENERATION_PATH}
    assert not (generation & PROFILE_AWARE_MODULES)


# ---------------------------------------------------------------------------
# 3. the eligibility gate that already exists, pinned
# ---------------------------------------------------------------------------

def test_the_chokepoint_refuses_an_unmappable_target():
    """Structural: the refusal is in the method, not in its caller."""
    code = _code_without_docstrings(
        inspect.getsource(GoalService.generate_anchored_suggestion))
    assert "is_activity_mappable" in code
    assert "GoalValidationError" in code
    # And it refuses BEFORE it writes a claim.
    assert code.index("is_activity_mappable") < code.index("claim")


def test_the_generation_service_refuses_the_three_non_generating_outcomes():
    """`no_unresolved_band`, `insufficient_known_evidence` and
    `no_supported_target` all return WITHOUT calling the chokepoint."""
    code = _code_without_docstrings(
        inspect.getsource(gen2.BaselineSuggestionGenerationV2Service
                          .generate_for_child))
    for outcome in ("OUTCOME_NO_UNRESOLVED_BAND",
                    "OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE",
                    "OUTCOME_NO_SUPPORTED_TARGET"):
        assert outcome in code, outcome
    # Each of the first two returns before the generation loop.
    generate_at = code.index("generate_anchored_suggestion")
    assert code.index("OUTCOME_NO_UNRESOLVED_BAND") < generate_at
    assert code.index("OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE") < generate_at


def test_only_mappable_rungs_are_passed_to_the_chokepoint():
    """The caller splits mappable from unsupported and generates only for the
    mappable ones — so an unsupported target never reaches the anchor write."""
    code = _code_without_docstrings(
        inspect.getsource(gen2.BaselineSuggestionGenerationV2Service
                          .generate_for_child))
    assert "for rung in rungs:" in code
    assert "unsupported" in code
    # `rungs` is the mappable half of `resolve_targets`, never `unsupported`.
    assert "for rung in unsupported" not in code
