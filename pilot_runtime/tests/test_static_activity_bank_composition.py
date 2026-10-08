"""0.6A-2 — the DEPLOYED browser composition wires the reviewed activity bank.

## The defect this file exists to prevent

`pilot_runtime.composition.build_runtime` called `build_application(...)` without
`activity_bank`, so the served application held `None` and
`_handle_week_one_release` fail-closed with a constant 403 — for the correct
managing clinician, with a correct body, in every real deployment. It was found
by deploying 0.6A-2 to staging and releasing a week, not by CI.

CI could not have found it. `test_week_one_release.py` builds the orchestrator
with its own bank, and `integration/test_week_one_route_chain_emulator.py` calls
`build_application(..., activity_bank=StaticActivityBank())` itself. Both prove
the ROUTE and the ORCHESTRATOR; neither touches the composition root, which is
the only place the production wiring decision is made.

So NOTHING here constructs `StaticActivityBank` directly. Every assertion reads
what `build_runtime` actually composed. A test that injected a bank would be
testing the same thing the existing suites already test, and would have passed
against the defective code.

## Why this file is in the integration job

Same CI-topology reason as `test_static_rung_composition.py`:
`pilot_runtime.composition` imports `firebase_admin` and
`google.cloud.firestore` at MODULE scope, so this cannot live in the
dependency-pure taxonomy job — it would fail to IMPORT and take the rest of
that job down with it.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from pilot_runtime.integration.static_activity_bank import (
    DEFAULT_ARTIFACT_PATH,
    StaticActivityBank,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSITION_PY = REPO_ROOT / "pilot_runtime" / "composition.py"

DEV_ENV = {"PILOT_ENVIRONMENT": "dev", "PILOT_DEV_AUTH_ENABLED": "true"}


def _runtime():
    """The REAL composition root. The only way a bank enters these tests."""
    from pilot_runtime.composition import build_runtime

    return build_runtime(dict(DEV_ENV), in_memory=True)


def test_the_built_runtime_has_a_non_none_activity_bank():
    """The headline. `activity_bank != None` in the real composition.

    Before this fix the deployed app passed nothing, so the Week 1 release
    route failed closed for a reason unrelated to the request.
    """
    runtime = _runtime()
    assert runtime.activity_bank is not None
    assert isinstance(runtime.activity_bank, StaticActivityBank)


def test_the_composed_application_holds_the_same_bank_object():
    """The WSGI application holds the SAME bank — not a second one, not None.

    This is the assertion that actually corresponds to the deployed failure:
    the transport reads `self._activity_bank`, and that attribute being `None`
    is what produced the 403.
    """
    runtime = _runtime()
    assert runtime.application._activity_bank is runtime.activity_bank
    assert runtime.application._activity_bank is not None


def test_the_composed_bank_is_usable_not_merely_present():
    """A bank that loaded but cannot serve a week is not a fixed deployment.

    `assert_release_ready` is the gate the release path depends on, so a
    present-but-unserved bank would move the same 403 one step later.
    """
    runtime = _runtime()
    bank = runtime.activity_bank
    # Raises `FamilyNotServed` if any declared family has no reviewed content.
    bank.assert_release_ready()

    families = sorted(bank._by_family)
    assert families, "the composed bank serves no activity family at all"
    # Reviewed content, reached the way the release path reaches it.
    templates = bank.templates_for_families((families[0],))
    assert templates, f"no reviewed template for {families[0]!r}"


def test_the_composed_bank_is_the_in_image_artifact():
    """It is the frozen artifact, not a stub and not a regenerated table.

    Pins the composed bank to the bytes that ship inside the image, so a test
    double or a locally regenerated artifact cannot satisfy this suite.
    """
    runtime = _runtime()
    assert DEFAULT_ARTIFACT_PATH.is_file()
    on_disk = json.loads(DEFAULT_ARTIFACT_PATH.read_text())
    assert runtime.activity_bank.artifact_digest == on_disk["artifact_digest"]


def test_composition_passes_activity_bank_to_build_application():
    """Structural guard: the keyword is PRESENT at the real call site.

    `build_application`'s own default is `None`, so omitting the keyword is
    silent — it produces a working deployment of every other route and a dead
    release route. An object-level assertion alone would not say WHY, and a
    future refactor could reintroduce the omission while some other seam kept
    `runtime.activity_bank` populated. This reads the one call that matters.
    """
    tree = ast.parse(COMPOSITION_PY.read_text())
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name)
             and node.func.id == "build_application"]
    assert len(calls) == 1, (
        f"expected exactly one build_application call site in "
        f"composition.py, found {len(calls)}")
    passed = {kw.arg for kw in calls[0].keywords}
    assert "activity_bank" in passed, (
        "composition.py calls build_application WITHOUT activity_bank; the "
        "Week 1 release route will fail closed with 403 in every deployment")
    # And it is not passed as a literal None.
    bank_kw = next(kw for kw in calls[0].keywords if kw.arg == "activity_bank")
    assert not (isinstance(bank_kw.value, ast.Constant)
                and bank_kw.value.value is None), \
        "composition.py passes activity_bank=None explicitly"


def test_the_bank_path_reaches_no_model_and_no_parent_package():
    """The bank is a static artifact read. No LLM, no Parent engine, no pandas.

    Mirrors the rung-source guard: a composition that pulled a model client or
    the Parent activity engine into the serving image would reintroduce exactly
    the dependency 0.6A-1 removed, and would fail in the real image.
    """
    code = (
        "import sys, json\n"
        "import pilot_runtime.composition as C\n"
        "rt = C.build_runtime({'PILOT_ENVIRONMENT': 'dev',\n"
        "                      'PILOT_DEV_AUTH_ENABLED': 'true'},\n"
        "                     in_memory=True)\n"
        "assert rt.activity_bank is not None\n"
        "print(json.dumps(sorted(\n"
        "    m for m in ('openai', 'anthropic', 'pandas', 'openpyxl',\n"
        "                'genex_core', 'parent_taxonomy')\n"
        "    if m in sys.modules)))\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == []
