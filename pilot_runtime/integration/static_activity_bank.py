"""pilot_runtime/integration/static_activity_bank.py — 0.6A-1.

The BROWSER Pilot's `ActivityBankSource`, backed by one generated JSON artifact
and nothing else.

## LOOKUP ONLY

Every method is a dict read plus validation. In particular NOTHING here:

    imports genex_core / parent_taxonomy        (not in the serving image)
    imports pandas / openpyxl                   (not installed)
    imports openai / anthropic                  (not installed)
    opens an .xlsx                              (none exist in the image)
    runs validate_activity                      (build-time only, by design)
    assembles or rewrites card text             (the artifact IS the content)

Validation happened offline against Parent's real validator; re-running it per
request would need the Parent package in the PHI-serving container, which is
the thing 0.5F-B froze out. The runtime's guarantee is the artifact digest plus
the CI drift gate, not a re-check.

## THE ARTIFACT IS VALIDATED, NOT TRUSTED

Each template is rebuilt through `ActivityTemplate.build`, which recomputes the
content-addressed id; a mismatch against the artifact's own key raises. So a
hand-edited instruction cannot keep its id, and a card whose id was also
adjusted still fails the drift gate, which regenerates from the curated source.

## IT REFUSES A PARTIALLY SERVED GOAL

`templates_for_families` raises `FamilyNotServed` when any requested family has
no templates. That is the live case: the pilot's 24m goal binds
`expressive_vocabulary_growth` (served) and `two_word_phrases` (no reviewed
content yet). Returning the served half would schedule single-word practice for
a two-word-combination target and report it as covered.

`release_ready` in the artifact says the same thing at the artifact level, and
`assert_release_ready` is the gate 0.6A-2 will call before releasing a week.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from pilot_backend.domain.activity_template import (
    CARD_FIELDS,
    ActivityTemplate,
    ActivityTemplateError,
)
from pilot_backend.integration.activity_bank import (
    ActivityBankError,
    FamilyNotServed,
)

#: Schema this adapter understands. Anything else is refused at load rather
#: than read optimistically.
SUPPORTED_ARTIFACT_SCHEMA = "pilot-activity-bank-v1"

#: Resolved from THIS file, not the process working directory, so a container
#: entrypoint starting from `/` finds the same bytes a test does.
DEFAULT_ARTIFACT_PATH = Path(__file__).resolve().parents[1] / "data" / \
    "activity_bank_talking_v1.json"


class StaticActivityBankError(ActivityBankError):
    """The artifact is absent, malformed, or fails its integrity check.

    A subclass of `ActivityBankError` so a broken bank fails CLOSED through the
    path callers already handle.
    """


def _canonical_json(payload: Any) -> str:
    """Must match `activity_bank_generator.canonical_json` exactly."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


class StaticActivityBank:
    """The generated activity bank, adapted to the activity-bank port."""

    def __init__(self, artifact_path: Optional[Path] = None) -> None:
        path = Path(artifact_path) if artifact_path is not None \
            else DEFAULT_ARTIFACT_PATH
        if not path.is_file():
            # Does not quote the path: the pilot's logging guard rejects
            # anything resembling a filesystem dump.
            raise StaticActivityBankError(
                "the activity bank artifact is not present")
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StaticActivityBankError(
                "the activity bank artifact could not be read") from exc
        if not isinstance(body, dict):
            raise StaticActivityBankError(
                "the activity bank artifact is malformed")

        if body.get("artifact_schema_version") != SUPPORTED_ARTIFACT_SCHEMA:
            raise StaticActivityBankError(
                "the activity bank artifact declares an unsupported schema")

        declared = body.get("artifact_digest")
        recomputed = self._digest(body)
        if not isinstance(declared, str) or declared != recomputed:
            # Catches accidental hand-editing. NOT a security control — anyone
            # who can edit the file can recompute this, which is why the CI
            # drift gate regenerates from the curated source instead.
            raise StaticActivityBankError(
                "the activity bank artifact fails its own integrity digest")

        self._domain = str(body.get("domain_key") or "").strip()
        if not self._domain:
            raise StaticActivityBankError(
                "the activity bank artifact declares no domain")

        raw = body.get("templates") or {}
        if not isinstance(raw, dict) or not raw:
            raise StaticActivityBankError(
                "the activity bank artifact holds no templates")

        self._templates: Dict[str, ActivityTemplate] = {}
        self._by_family: Dict[str, list] = {}
        for template_id, entry in raw.items():
            try:
                template = ActivityTemplate.build(
                    activity_family_ref=entry["activity_family_ref"],
                    source_tier=entry["source_tier"],
                    source_pool_ref=entry["source_pool_ref"],
                    card={f: entry[f] for f in CARD_FIELDS})
            except (KeyError, TypeError, ActivityTemplateError) as exc:
                raise StaticActivityBankError(
                    "the activity bank artifact has an invalid template"
                ) from exc
            if template.activity_template_id != template_id:
                # The id is a digest of the content, so this fires whenever a
                # card's text was edited without re-deriving its key.
                raise StaticActivityBankError(
                    "a template entry disagrees with its own identifier")
            self._templates[template_id] = template
            self._by_family.setdefault(
                template.activity_family_ref, []).append(template_id)

        self._required = tuple(body.get("required_families") or ())
        self._unserved = tuple(body.get("unserved_required_families") or ())
        self._release_ready = bool(body.get("release_ready"))
        self._digest_value = recomputed
        self._provenance = dict(body.get("provenance") or {})

    # -- integrity ---------------------------------------------------------

    @staticmethod
    def _digest(body: Dict[str, Any]) -> str:
        without = {k: v for k, v in body.items() if k != "artifact_digest"}
        return hashlib.sha256(
            _canonical_json(without).encode("utf-8")).hexdigest()

    @property
    def artifact_digest(self) -> str:
        return self._digest_value

    @property
    def provenance(self) -> Dict[str, Any]:
        """Recorded source SHAs and versions. Read-only copy."""
        return dict(self._provenance)

    @property
    def release_ready(self) -> bool:
        """Whether every REQUIRED family has reviewed content."""
        return self._release_ready

    @property
    def unserved_required_families(self) -> Tuple[str, ...]:
        return self._unserved

    # -- the port ----------------------------------------------------------

    def served_families(self) -> Tuple[str, ...]:
        return tuple(sorted(self._by_family))

    def templates_for_families(self, families: Sequence[str]
                               ) -> Tuple[ActivityTemplate, ...]:
        """Every template for these families, or a refusal.

        Ordered by `activity_template_id` — a content digest — so the order
        depends only on authored content, never on artifact key order.
        """
        wanted = [(f or "").strip() for f in families]
        wanted = [f for f in wanted if f]
        if not wanted:
            raise FamilyNotServed("no activity family was requested")
        missing = sorted(set(wanted) - set(self._by_family))
        if missing:
            raise FamilyNotServed(
                "no reviewed activity content exists for: "
                + ", ".join(missing))
        ids = sorted({tid for f in wanted for tid in self._by_family[f]})
        return tuple(self._templates[tid] for tid in ids)

    def assert_release_ready(self) -> None:
        """The gate 0.6A-2 must call before releasing a week.

        Separate from `templates_for_families` so "can this pilot release at
        all?" is answerable without naming a goal's families.
        """
        if not self._release_ready:
            raise FamilyNotServed(
                "the activity bank has no reviewed content for: "
                + ", ".join(self._unserved))


def build_static_activity_bank(artifact_path: Optional[Path] = None):
    """The composition seam. Raises rather than returning a degraded bank.

    Deliberately NOT returning None on a missing artifact: `None` would become
    a silent "no activities configured" that looks like a planning outcome
    instead of a build defect — the same mistake 0.5F-B found in the rung path.
    """
    return StaticActivityBank(artifact_path)
