"""The OFFLINE generation path, and the narrowness of the two images.

Dependency-pure: uses `InMemoryGoldStandardRungSource`, so no pandas, no
workbook and no `genex-parent` on the path. The LIVE bridge is covered by
`test_parent_gold_standard_adapter.py`; what is under test here is the step
between a resolved rung and the frozen `generate_suggestions` boundary, plus
the packaging guarantees the founder asked be kept distinguishable.
"""

from __future__ import annotations

import pathlib

import pytest

from pilot_backend.goals.suggestion_engine import EvidenceSource
from pilot_backend.integration.gold_standard_source import (
    InMemoryGoldStandardRungSource,
    RungTarget,
)
from pilot_runtime.deploy.generate_suggestions_job import (
    observed_domains_with_rungs,
)

from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
)

#: Resolved locally rather than imported from the cross-system test module,
#: which puts `genex-parent` on `sys.path` at import time. This file must stay
#: runnable in the dependency-pure job, where that content is absent.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "pilot_runtime" / "deploy"
SLP = "talking_and_communicating"


def _rung(months: int = 30, milestone: str = "says about 50 words"):
    return CanonicalRung.build(
        domain_key=SLP, source_rung_months=months, milestone_text=milestone,
        subdomain="expressive_language",
        family_bindings=[ActivityFamilyBinding(
            family_ref="expressive_vocabulary_growth",
            allowed_domains=(SLP,))],
        track_subdomains=("early_vocalization_and_babbling",
                          "expressive_language"),
        track_families=(),
        taxonomy_version="activity_family_taxonomy_v1",
        baseline_version="parent-2.4-functional-baseline-v1",
    )


def _observation(months, milestone, key=SLP):
    return (key, True, EvidenceSource.CAREGIVER_REPORTED_MILESTONE, "talking", "many", True,
            months, milestone)


# ---------------------------------------------------------------------------
# a resolved rung is attached; an unresolved one leaves the domain unanchored
# ---------------------------------------------------------------------------

def test_a_resolvable_observation_gets_its_rung_attached():
    rung = _rung()
    source = InMemoryGoldStandardRungSource(rungs=(rung,))
    domains, notes = observed_domains_with_rungs(
        source, [_observation(30, "says about 50 words")])
    assert len(domains) == 1
    assert domains[0].canonical_rung is rung
    assert notes == ()


def test_an_unresolvable_observation_is_passed_through_unanchored():
    """The fail-closed outcome. The domain still reaches the engine — it is
    not dropped — but with no rung, so an approved goal has no anchor and
    allocation refuses it."""
    source = InMemoryGoldStandardRungSource(rungs=())
    domains, notes = observed_domains_with_rungs(
        source, [_observation(30, "says words like I me or we")])
    assert len(domains) == 1
    assert domains[0].canonical_rung is None
    assert len(notes) == 1 and "UNANCHORED" in notes[0]


def test_an_unresolvable_domain_is_never_given_another_rung():
    """The substitution the founder ruled out. A rung exists in the source at
    a DIFFERENT month; the unresolvable domain must not receive it."""
    source = InMemoryGoldStandardRungSource(rungs=(_rung(months=30),))
    domains, _ = observed_domains_with_rungs(
        source, [_observation(36, "says first name when asked")])
    assert domains[0].canonical_rung is None


def test_a_refused_target_leaves_the_domain_unanchored():
    target = RungTarget(SLP, 30, "says words like I me or we")
    source = InMemoryGoldStandardRungSource(rungs=(_rung(),),
                                            unmappable=(target,))
    domains, notes = observed_domains_with_rungs(
        source, [_observation(30, "says words like I me or we")])
    assert domains[0].canonical_rung is None
    assert notes


def test_an_observation_naming_no_target_is_unanchored_not_an_error():
    source = InMemoryGoldStandardRungSource(rungs=(_rung(),))
    domains, notes = observed_domains_with_rungs(
        source, [(SLP, False, EvidenceSource.CAREGIVER_REPORTED_MILESTONE, "", "", False,
                  None, None)])
    assert domains[0].canonical_rung is None
    assert "names no target rung" in notes[0]


def test_a_mixed_batch_anchors_only_what_resolves():
    """One domain failing must not cost the others their anchors."""
    source = InMemoryGoldStandardRungSource(rungs=(_rung(months=30),))
    domains, notes = observed_domains_with_rungs(source, [
        _observation(30, "says about 50 words"),
        _observation(36, "says first name when asked"),
    ])
    assert domains[0].canonical_rung is not None
    assert domains[1].canonical_rung is None
    assert len(notes) == 1


def test_no_note_quotes_the_milestone_text():
    """Notes go to a job log. Milestone text is clinical content; the domain
    and the months are enough to find the rung in the workbook."""
    source = InMemoryGoldStandardRungSource(rungs=())
    _, notes = observed_domains_with_rungs(
        source, [_observation(30, "says words like I me or we")])
    assert notes
    for note in notes:
        assert "says words like" not in note
        assert "I me or we" not in note


def test_domain_order_is_preserved():
    """The engine ranks by its own rule, but the snapshot it receives must
    reflect the observation order it was given, not resolution order."""
    source = InMemoryGoldStandardRungSource(rungs=(_rung(months=30),))
    domains, _ = observed_domains_with_rungs(source, [
        _observation(36, "unresolvable one"),
        _observation(30, "says about 50 words"),
        _observation(48, "unresolvable two"),
    ])
    assert [d.canonical_rung is not None for d in domains] == \
        [False, True, False]


# ---------------------------------------------------------------------------
# re-running generation must not duplicate a month
# ---------------------------------------------------------------------------

class _Suggestion:
    def __init__(self, cycle_month):
        self.cycle_month = cycle_month


class _RecordingGoals:
    """The two GoalService methods the job calls, and nothing else."""

    def __init__(self, existing=()):
        self.existing = list(existing)
        self.generated = []

    def list_suggestions(self, principal, child_id):
        return tuple(self.existing)

    def generate_suggestions(self, principal, child_id, snapshot,
                             request_id=""):
        self.generated.append(snapshot)
        return tuple(_Suggestion(snapshot.cycle_month)
                     for _ in snapshot.domains)


def _generate(goals, observations, cycle_month="2026-10"):
    from pilot_runtime.deploy.generate_suggestions_job import (
        generate_for_child)

    return generate_for_child(
        goals=goals, principal=object(), child_id="child_x",
        cycle_month=cycle_month,
        source=InMemoryGoldStandardRungSource(rungs=(_rung(),)),
        observations=observations)


def test_a_second_run_for_the_same_child_month_is_refused():
    """`generate_suggestions` only ever CREATES. Running twice would leave two
    parallel candidate sets for one month rather than replacing the first —
    including any a clinician had already acted on."""
    goals = _RecordingGoals(existing=[_Suggestion("2026-10")])
    with pytest.raises(RuntimeError, match="already exist"):
        _generate(goals, [_observation(30, "says about 50 words")])
    assert goals.generated == []


def test_a_different_month_is_not_blocked_by_an_earlier_one():
    goals = _RecordingGoals(existing=[_Suggestion("2026-09")])
    _generate(goals, [_observation(30, "says about 50 words")],
              cycle_month="2026-10")
    assert len(goals.generated) == 1
    assert goals.generated[0].cycle_month == "2026-10"


def test_the_snapshot_handed_to_the_engine_carries_the_resolved_rung():
    """The job's whole contribution: the frozen boundary receives a snapshot
    whose domains already carry canonical provenance. It writes no anchor
    itself."""
    goals = _RecordingGoals()
    _generate(goals, [_observation(30, "says about 50 words")])
    snapshot = goals.generated[0]
    assert snapshot.child_id == "child_x"
    assert snapshot.domains[0].canonical_rung is not None
    assert snapshot.domains[0].canonical_rung.rung_ref == _rung().rung_ref


# ---------------------------------------------------------------------------
# A. the SERVING image stays narrow — unchanged by this slice
# ---------------------------------------------------------------------------

#: The served API image's dependency surface, pinned verbatim. 0.5E-B adds the
#: Parent workbook to the GENERATION image only; if any of these three lines
#: changed, the PHI-reviewed web service grew a dependency.
SERVING_REQUIREMENTS = {
    "firebase-admin==7.5.0",
    "google-cloud-firestore==2.28.0",
    "gunicorn==23.0.0",
}


def test_the_serving_image_requirements_are_unchanged():
    lines = {line.strip() for line
             in (DEPLOY / "requirements.txt").read_text().splitlines()
             if line.strip() and not line.strip().startswith("#")}
    assert lines == SERVING_REQUIREMENTS, lines


@pytest.mark.parametrize("banned", ["pandas", "openpyxl", "numpy", "openai",
                                    "streamlit", "requests"])
def test_the_serving_image_installs_no_workbook_or_model_dependency(banned):
    text = (DEPLOY / "requirements.txt").read_text()
    installed = [line.strip() for line in text.splitlines()
                 if line.strip() and not line.strip().startswith("#")]
    assert not any(line.lower().startswith(banned) for line in installed), banned


def test_the_serving_dockerfile_copies_no_parent_content():
    """The served image copies two packages. `genex-parent` must not appear in
    any COPY — that is the whole reason the generation image exists."""
    text = (DEPLOY / "Dockerfile").read_text()
    copies = [line.strip() for line in text.splitlines()
              if line.strip().upper().startswith("COPY")]
    assert copies, "no COPY lines found — the guard would be vacuous"
    for line in copies:
        assert "genex-parent" not in line, line


def test_the_serving_build_context_admits_no_parent_content():
    text = (DEPLOY / "gcloudignore").read_text()
    admitted = [line.strip() for line in text.splitlines()
                if line.strip().startswith("!")]
    assert admitted, "no allowlist entries found — guard would be vacuous"
    for line in admitted:
        assert "genex-parent" not in line, line


# ---------------------------------------------------------------------------
# B. the GENERATION image has what it needs and nothing more
# ---------------------------------------------------------------------------

GENERATION_REQUIREMENTS = {
    "firebase-admin==7.5.0",
    "google-cloud-firestore==2.28.0",
    "pandas==3.0.3",
    "openpyxl==3.1.5",
}


def test_the_generation_image_pins_exactly_its_four_dependencies():
    lines = {line.strip() for line
             in (DEPLOY / "requirements-generation.txt").read_text().splitlines()
             if line.strip() and not line.strip().startswith("#")}
    assert lines == GENERATION_REQUIREMENTS, lines


def test_the_generation_image_installs_no_model_client_and_no_web_server():
    """`openai` absent is a SAFETY property, not a size one: Parent's model
    client import is lazy and env-gated, so omitting the package is what makes
    activation impossible. `gunicorn` absent says this is a job, not a
    service that could be deployed by mistake."""
    lines = (DEPLOY / "requirements-generation.txt").read_text()
    installed = [line.strip() for line in lines.splitlines()
                 if line.strip() and not line.strip().startswith("#")]
    for banned in ("openai", "gunicorn", "anthropic", "streamlit"):
        assert not any(line.lower().startswith(banned) for line in installed), \
            banned


def test_the_two_images_agree_on_the_sdk_pins():
    """The job writes records the API reads. A version skew between them would
    be a serialisation difference nothing else would catch."""
    shared = {"firebase-admin==7.5.0", "google-cloud-firestore==2.28.0"}
    assert shared <= SERVING_REQUIREMENTS
    assert shared <= GENERATION_REQUIREMENTS


def test_the_generation_dockerfile_copies_only_the_measured_parent_subset():
    """Three paths under genex-parent, and only three. `api/` and `webapp/`
    are the Parent service and frontend and must never enter a pilot artifact.
    """
    text = (DEPLOY / "Dockerfile.generation").read_text()
    copies = [line.strip() for line in text.splitlines()
              if line.strip().upper().startswith("COPY")]
    parent_copies = [line for line in copies if "genex-parent" in line]
    assert len(parent_copies) == 3, parent_copies
    joined = " ".join(parent_copies)
    assert "parent_taxonomy/" in joined
    assert "genex_core/" in joined
    assert "data/parent_2_4/" in joined
    # Scanned over COPY lines only: the Dockerfile's prose deliberately NAMES
    # the paths it excludes, so a whole-file substring scan would flag its own
    # documentation.
    for forbidden in ("genex-parent/api", "genex-parent/webapp",
                      "genex-parent/tests", "genex-parent/notebooks"):
        assert not any(forbidden in line for line in parent_copies), forbidden


def test_the_generation_dockerfile_does_not_copy_the_frozen_snapshot():
    """The immutable c2b6735d Gold Standard snapshot sits at the data ROOT and
    is not read by the generation path. Copying only `data/parent_2_4/` keeps
    it out, which is also why the copy must not be widened to `data/`."""
    text = (DEPLOY / "Dockerfile.generation").read_text()
    assert "COPY genex-parent/data/ " not in text
    assert "cdc_milestones_with_bridges_family_cleaned_final_app_ready" not in text


def test_the_generation_image_is_a_job_with_no_default_command():
    """No CMD and no ENTRYPOINT: an image that cannot start on its own cannot
    be mistaken for a service and deployed as one."""
    lines = [line.strip().upper()
             for line in (DEPLOY / "Dockerfile.generation").read_text().splitlines()]
    assert not any(line.startswith("CMD") for line in lines)
    assert not any(line.startswith("ENTRYPOINT") for line in lines)


def test_the_generation_build_admits_parent_but_not_the_parent_service():
    ignore = (DEPLOY / "gcloudignore-generation").read_text()
    admitted = [line.strip() for line in ignore.splitlines()
                if line.strip().startswith("!")]
    assert any("parent_taxonomy" in line for line in admitted)
    assert any("genex_core" in line for line in admitted)
    assert any("data/parent_2_4" in line for line in admitted)
    for forbidden in ("!genex-parent/api", "!genex-parent/webapp",
                      "!genex-parent/data/**"):
        assert forbidden not in ignore, forbidden


def test_the_two_build_configs_publish_different_images():
    serving = (DEPLOY / "cloudbuild.yaml").read_text()
    generation = (DEPLOY / "cloudbuild-generation.yaml").read_text()
    assert "pilot/pilot-api" in serving
    assert "pilot/pilot-generation" in generation
    assert "pilot/pilot-api" not in generation
    assert "Dockerfile.generation" in generation
    assert "Dockerfile.generation" not in serving


def test_the_generation_entrypoint_takes_no_rung_fields_from_its_caller():
    """Identity and provenance stay server-derived. The job accepts a child id
    and a cycle month; a domain, milestone, months value, family or rung ref
    passed in would make the caller authoritative for provenance."""
    source = (DEPLOY / "generate_suggestions_job.py").read_text()
    assert "<child_id> <cycle_month>" in source
    for forbidden in ("--rung-ref", "--domain", "--milestone", "--family",
                      "--months", "--track-ref"):
        assert forbidden not in source, forbidden


def test_the_job_module_imports_no_workbook_library_directly():
    """The job goes through the PORT. pandas belongs to the adapter, so the
    job stays testable without the workbook — as this file demonstrates."""
    import ast

    tree = ast.parse((DEPLOY / "generate_suggestions_job.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported, "AST walk found no imports — the guard would be vacuous"
    for banned in ("pandas", "openpyxl", "genex_core", "parent_taxonomy",
                   "openai"):
        assert banned not in imported, banned
