"""pilot_runtime/integration/static_developmental_ladder.py — P0-B2a.

The BROWSER Pilot's developmental ladder, loaded from ONE generated artifact.
Lookup only: no pandas, no workbook, no Parent package, no model. The artifact
is schema-checked, digest-checked, rebuilt through `ladder_from_artifact`
(which recomputes the envelope ref AND the automatic-transition rule, refusing
an artifact that enables anything the rule does not), and cross-checked
against the reviewed activity bank it was built from: every node pool must be
exactly the bank's reviewed templates of that node's family.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from pilot_backend.domain.developmental_ladder import (
    LADDER_ARTIFACT_SCHEMA,
    DevelopmentalLadder,
    LadderError,
    ladder_from_artifact,
)

DEFAULT_ARTIFACT_PATH = Path(__file__).resolve().parents[1] / "data" / \
    "developmental_ladder_talking_v1.json"


class StaticLadderError(LadderError):
    """The ladder artifact is absent, malformed, or fails a check."""


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def load_developmental_ladder(artifact_path: Optional[Path] = None, *,
                              activity_bank: Any = None
                              ) -> DevelopmentalLadder:
    path = Path(artifact_path) if artifact_path is not None \
        else DEFAULT_ARTIFACT_PATH
    if not path.is_file():
        raise StaticLadderError("the developmental ladder artifact is absent")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StaticLadderError("the ladder artifact could not be read") from exc
    if not isinstance(body, dict) or \
            body.get("artifact_schema_version") != LADDER_ARTIFACT_SCHEMA:
        raise StaticLadderError("the ladder artifact has an unsupported schema")
    without = {k: v for k, v in body.items() if k != "artifact_digest"}
    digest = hashlib.sha256(_canonical_json(without).encode("utf-8")).hexdigest()
    if body.get("artifact_digest") != digest:
        raise StaticLadderError("the ladder artifact fails its own digest")
    ladder = ladder_from_artifact(body)

    if activity_bank is not None:
        # The pools must be EXACTLY the reviewed bank's templates for each
        # node's family — never a superset, never a stale subset.
        for node in ladder.nodes:
            if not node.pool_family_ref:
                continue
            served = sorted(t.activity_template_id for t in
                            activity_bank.templates_for_families(
                                [node.pool_family_ref]))
            if sorted(node.pool_template_ids) != served:
                raise StaticLadderError(
                    "a ladder node pool disagrees with the reviewed bank")
    return ladder


def build_static_developmental_ladder(artifact_path: Optional[Path] = None, *,
                                      activity_bank: Any = None
                                      ) -> DevelopmentalLadder:
    """Composition seam. Raises rather than returning a degraded ladder."""
    return load_developmental_ladder(artifact_path, activity_bank=activity_bank)
