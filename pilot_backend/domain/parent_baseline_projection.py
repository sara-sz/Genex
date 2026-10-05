"""pilot_backend/domain/parent_baseline_projection.py — the A2 projection.

Parent remains the system of record for a functional baseline. This is the
Pilot's IMMUTABLE, MINIMUM-NECESSARY copy of one finalized baseline, written
once by an authenticated Parent service and never rewritten.

## SEVEN FIELDS, AND WHY EXACTLY SEVEN

0.5F-B's target rule needs: the floor (`routing_anchor_months`), the ceiling
guard (`not_demonstrated_months`), the declared track (reconstructed from
`area_id` + `entry_choice_id`), the ladder's domain, the `status` to fail
closed on, and `baseline_version` to stamp provenance. Measured against the
real engine: `_track_for` reads only area and choice, and
`_nearest_rung`/`_step`/`ladder_months` take no age at all.

So four fields of the Parent record are deliberately NOT here:

  asked[]               the richest clinical content in the baseline — a
                        per-rung yes/no about one child — and the rule never
                        reads it
  chronological_months  used by the engine only for the "Not sure" start and
                        the age-relevance test, both already resolved before
                        finalization; also the most identifying value
  demonstrated_months   the floor is already `routing_anchor_months`
  entry_anchor_months   derivable from `entry_choice_id`

`source_record_digest` gives better audit than a partial copy would: it proves
exactly which finalized record this came from without carrying its contents.

## WHAT THE PILOT DERIVES RATHER THAN ACCEPTS

`projection_id`, `child_id`, `source_system` and `projected_at` are all
produced here or by the service. A caller supplies none of them. In particular
`child_id` is resolved ONLY through the existing `SourceSystemLink`, so the
authenticated Parent service cannot name a child even if it wanted to.

## IMMUTABILITY IS STRUCTURAL

An A1 finalized baseline is itself immutable, so a second projection of the
same source with a DIFFERENT digest is not a new version — it is an integrity
failure. The repository therefore has no update, set or delete, and the
service refuses a digest conflict rather than writing a second row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple

from .entities import utc_now
from .goal_vocabulary import require_canonical_domain
from .identity_claims import key_digest
from .source_link import SourceSystem

SCHEMA_VERSION = "parent-baseline-projection-v1"

#: The projection id prefix, matching the typed-id convention used across the
#: pilot (`chld_`, `clgl_`, `gsug_` …).
PROJECTION_ID_PREFIX = "pbpj_"

#: The ONLY domain 0.5F-A2 projects. The Parent side exposes only this one
#: too, so a mismatch here would mean the two sides had drifted.
SUPPORTED_DOMAINS: Tuple[str, ...] = ("talking_and_communicating",)

#: Baseline statuses the engine can finalize to. Restated rather than imported
#: because `pilot_backend` cannot import `genex-parent`; the cross-system CI
#: gate compares this tuple against `BaselineStatus` in the real engine.
VALID_STATUSES: Tuple[str, ...] = (
    "UNRESOLVED", "BOUNDED", "AGE_RELEVANT", "EMERGING", "CONTRADICTORY",
)

#: Exactly the inbound projection field names. The allowlist IS the contract:
#: anything else is refused rather than ignored, because a silently dropped
#: field would leave the caller believing it had sent something that mattered.
PROJECTION_FIELDS: Tuple[str, ...] = (
    "domain", "area_id", "entry_choice_id", "routing_anchor_months",
    "not_demonstrated_months", "status", "baseline_version",
)

#: Field names that must never appear in an inbound projection. Listed
#: explicitly so the refusal names what was wrong, and so a reviewer can see
#: at a glance which Parent values are considered out of bounds.
FORBIDDEN_FIELDS: Tuple[str, ...] = (
    "child_id", "owner_uid", "uid", "external_owner_ref", "parent_uid",
    "child_name", "name", "chronological_months", "asked", "diagnosis",
    "diagnosis_or_condition", "concern", "qna", "schedules",
    "weekly_schedule", "activities", "activity_banks", "demonstrated_months",
    "entry_anchor_months", "dev_age", "projection_id", "source_system",
    "projected_at", "brain_state",
)


class ProjectionError(ValueError):
    """Base for every projection refusal. PHI-safe message, always."""

    PHI_SAFE_MESSAGE = True


class ProjectionValidationError(ProjectionError):
    """The inbound payload is not a valid seven-field projection."""


class ProjectionIntegrityError(ProjectionError):
    """A projection for this immutable source exists with a DIFFERENT digest.

    Separate from a validation failure on purpose. The payload may be
    perfectly well formed; what is wrong is that the Parent record it claims
    to copy is supposed to be immutable and two different digests cannot both
    be it. Silently versioning would record a history the source does not
    have.
    """


def canonical_source_digest(record: Mapping[str, Any]) -> str:
    """The deterministic digest of a finalized Parent baseline record.

    Computed over the FULL canonical `BaselineRecord.to_state()` — not the
    seven projected fields — so it attests the whole source record, including
    the `asked` history the Pilot deliberately does not store. That is the
    point: the Pilot can prove which record it was given without holding it.

    `json.dumps(..., sort_keys=True, separators=...)` with `ensure_ascii=False`
    is the canonical form. Sorting removes dict-order dependence; the compact
    separators remove whitespace drift; `ensure_ascii=False` keeps the digest
    stable whether or not a milestone ever contains a non-ASCII character.
    Both sides compute it the same way, and a cross-system test pins that.
    """
    if not isinstance(record, Mapping):
        raise ProjectionValidationError("a source record must be a mapping")
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_str(payload: Mapping[str, Any], name: str, *,
                 limit: int = 128) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProjectionValidationError(f"{name} is required")
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise ProjectionValidationError(f"{name} is too long")
    return cleaned


def _require_months(payload: Mapping[str, Any], name: str) -> Optional[int]:
    """A rung month, or None.

    None is MEANINGFUL and must survive: `routing_anchor_months` is None for
    an UNRESOLVED or CONTRADICTORY baseline, and `not_demonstrated_months` is
    None when nothing was refused. Coercing either to 0 would turn "no
    evidence" into a rung, which is the one thing the 0.4 engine exists to
    prevent.

    `bool` is rejected explicitly because `True == 1` would otherwise pass as
    a one-month rung.
    """
    if name not in payload:
        raise ProjectionValidationError(f"{name} is required")
    value = payload[name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionValidationError(f"{name} must be an integer or null")
    if value <= 0 or value > 300:
        raise ProjectionValidationError(f"{name} is out of range")
    return value


def validate_projection_payload(payload: Mapping[str, Any]
                                ) -> Mapping[str, Any]:
    """The seven fields, validated, or a refusal. No coercion of unknowns.

    The Pilot treats the authenticated Parent service as authoritative for
    SOURCE PROVENANCE — which session, which digest — and independently
    validates the SHAPE. Those are different trusts: provenance cannot be
    checked from here, but a malformed status or an extra field can.
    """
    if not isinstance(payload, Mapping):
        raise ProjectionValidationError("a projection must be an object")

    present = set(payload)
    for forbidden in FORBIDDEN_FIELDS:
        if forbidden in present:
            raise ProjectionValidationError("projection contains a field the "
                                            "Pilot derives or forbids")
    unexpected = present - set(PROJECTION_FIELDS)
    if unexpected:
        raise ProjectionValidationError("projection contains unexpected fields")
    missing = set(PROJECTION_FIELDS) - present
    if missing:
        raise ProjectionValidationError("projection is missing fields")

    try:
        domain = require_canonical_domain(_require_str(payload, "domain"))
    except ProjectionValidationError:
        raise
    except Exception as exc:
        # `require_canonical_domain` raises its own UnknownDomainError.
        # Re-raised as a projection failure so the transport maps it by TYPE
        # rather than catching a stray exception class it does not model.
        raise ProjectionValidationError("domain is not canonical") from exc
    if domain not in SUPPORTED_DOMAINS:
        raise ProjectionValidationError("domain is not supported")

    status = _require_str(payload, "status", limit=32)
    if status not in VALID_STATUSES:
        raise ProjectionValidationError("status is not a baseline status")

    return {
        "domain": domain,
        "area_id": _require_str(payload, "area_id", limit=64),
        "entry_choice_id": _require_str(payload, "entry_choice_id", limit=64),
        "routing_anchor_months": _require_months(payload,
                                                 "routing_anchor_months"),
        "not_demonstrated_months": _require_months(payload,
                                                   "not_demonstrated_months"),
        "status": status,
        "baseline_version": _require_str(payload, "baseline_version"),
    }


def projection_id_for(source_session_id: str, domain: str,
                      source_record_digest: str) -> str:
    """The DETERMINISTIC id, so an exact retry collides instead of duplicating.

    Keyed on (session, domain, digest) via `key_digest`, which NUL-joins its
    parts — so ("ab", "c") and ("a", "bc") cannot collide into one id, which a
    plain concatenation would allow.

    Making the id a function of the content is what makes idempotency a
    PROPERTY rather than a check someone has to run first: the second create
    addresses the same document, and a create-only repository refuses it.
    """
    return PROJECTION_ID_PREFIX + key_digest(
        source_session_id, domain, source_record_digest)[:32]


@dataclass(frozen=True)
class ParentBaselineProjection:
    """One immutable projection of one finalized Parent baseline.

    Field order matters only for readability; the codec drives off names.
    """

    projection_id: str
    child_id: str
    source_system: SourceSystem
    source_session_id: str
    source_record_digest: str
    #: The seven allowlisted baseline fields, flattened rather than nested so
    #: the stored document is directly queryable and the codec stays simple.
    domain: str
    area_id: str
    entry_choice_id: str
    status: str
    baseline_version: str
    routing_anchor_months: Optional[int] = None
    not_demonstrated_months: Optional[int] = None
    projected_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not (self.projection_id or "").startswith(PROJECTION_ID_PREFIX):
            raise ProjectionValidationError("malformed projection id")
        if not (self.child_id or "").strip():
            raise ProjectionValidationError("a projection requires a child")
        if not isinstance(self.source_system, SourceSystem):
            raise ProjectionValidationError(
                "source_system must be a SourceSystem")
        if self.source_system is not SourceSystem.PARENT:
            # A2 projects Parent baselines and nothing else. A therapist-origin
            # projection would be a different record with a different trust
            # story, so it is refused here rather than accommodated.
            raise ProjectionValidationError(
                "only Parent baselines are projected")
        if not (self.source_session_id or "").strip():
            raise ProjectionValidationError(
                "a projection requires its source session")
        if len(self.source_record_digest or "") != 64:
            raise ProjectionValidationError(
                "source_record_digest must be a sha256 hex digest")
        require_canonical_domain(self.domain)
        if self.domain not in SUPPORTED_DOMAINS:
            raise ProjectionValidationError("domain is not supported")
        if self.status not in VALID_STATUSES:
            raise ProjectionValidationError("status is not a baseline status")

        expected = projection_id_for(self.source_session_id, self.domain,
                                     self.source_record_digest)
        if self.projection_id != expected:
            # The id is a function of the content, so a mismatch means the two
            # disagree about which source this is. Recomputing and comparing
            # here makes that impossible to persist.
            raise ProjectionValidationError(
                "projection id does not match its source identity")

    @property
    def has_routing_anchor(self) -> bool:
        """Whether this baseline resolved to a planning anchor at all.

        A DERIVED property, absent from the codec, so no stored copy can drift.
        False for UNRESOLVED and CONTRADICTORY — the states 0.5F-B must fail
        closed on rather than plan from.
        """
        return self.routing_anchor_months is not None

    @staticmethod
    def build(*, child_id: str, source_session_id: str,
              source_record_digest: str, projection: Mapping[str, Any],
              now: Optional[datetime] = None) -> "ParentBaselineProjection":
        """The only constructor callers should use.

        `projection` must already have been through
        `validate_projection_payload`; this re-reads it by name rather than
        splatting, so an unexpected key cannot ride in as a constructor
        argument.
        """
        return ParentBaselineProjection(
            projection_id=projection_id_for(
                source_session_id, projection["domain"], source_record_digest),
            child_id=child_id,
            source_system=SourceSystem.PARENT,
            source_session_id=source_session_id,
            source_record_digest=source_record_digest,
            domain=projection["domain"],
            area_id=projection["area_id"],
            entry_choice_id=projection["entry_choice_id"],
            status=projection["status"],
            baseline_version=projection["baseline_version"],
            routing_anchor_months=projection["routing_anchor_months"],
            not_demonstrated_months=projection["not_demonstrated_months"],
            projected_at=now or utc_now(),
        )
