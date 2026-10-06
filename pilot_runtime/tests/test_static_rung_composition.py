"""0.5F-B Option C — the DEPLOYED browser composition wires the static table.

Split out of `test_static_rung_table.py` for a CI-topology reason, not a
stylistic one. `pilot_runtime.composition` imports `firebase_admin` and
`google.cloud.firestore` at MODULE scope, so importing it needs the cloud SDKs.
The dependency-pure taxonomy job installs exactly pytest, pandas and openpyxl —
it is the job that proves the pilot touches no SDK — so a composition test
placed there does not fail on its assertion, it fails to IMPORT, and takes
every later step in that job down with it. That is what happened on the first
hosted run of this slice.

These tests therefore belong to the integration job, which installs
firebase-admin and google-cloud-firestore for exactly this purpose.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pilot_runtime.integration.static_rung_source import StaticRungTableSource

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_built_runtime_has_a_non_none_rung_source():
    """The headline of Option C: `rung_source != None` in the real composition.

    Before this slice the deployed app passed nothing, so the generation route
    failed closed with 403 for a reason unrelated to the request.
    """
    from pilot_runtime.composition import build_runtime

    runtime = build_runtime({"PILOT_ENVIRONMENT": "dev",
                             "PILOT_DEV_AUTH_ENABLED": "true"},
                            in_memory=True)
    assert runtime.rung_source is not None
    assert isinstance(runtime.rung_source, StaticRungTableSource)
    # And the WSGI application holds the SAME object — not a second one, and
    # not None.
    assert runtime.application._rung_source is runtime.rung_source


def test_the_served_entrypoint_still_does_not_reach_the_live_adapter():
    """Composition must use the STATIC source, never the workbook adapter.

    Preserved from 0.5E-B. The live adapter needs pandas and the Parent
    package; a composition that imported it would reintroduce the dependency
    Option C exists to avoid, and would fail at runtime in the serving image.
    """
    import subprocess

    code = (
        "import sys, json\n"
        "import pilot_runtime.composition as C\n"
        "C.build_runtime({'PILOT_ENVIRONMENT': 'dev',\n"
        "                 'PILOT_DEV_AUTH_ENABLED': 'true'}, in_memory=True)\n"
        "print(json.dumps(\n"
        "    'pilot_runtime.integration.parent_gold_standard_source'\n"
        "    in sys.modules))\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) is False


