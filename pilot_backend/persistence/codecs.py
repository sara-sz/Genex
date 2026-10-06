"""pilot_backend/persistence/codecs.py — explicit document serialization.

## No silent field dropping, in either direction

Encoding iterates the dataclass's OWN field list, so a field added to an entity
is written automatically and cannot be forgotten. Decoding requires the
document's key set to equal the expected field set EXACTLY — an unknown key and
a missing key are both errors.

That strictness is the point. The failure this prevents is a schema change that
half-lands: a field added to the model, written by the new code, silently
dropped by an old decoder, and then written back as absent. For a relationship
row or a finalized revision, that is data loss that looks like success.

Loud decode failures are also how a corrupted or hand-edited document announces
itself, instead of quietly deserialising into a record with default values —
and a `ConnectionStatus` defaulting to ACTIVE on a malformed row would be an
authorization failure, not a data failure.

## Timestamps

Always stored as ISO-8601 strings with an explicit UTC offset, always decoded
back to timezone-aware datetimes. A naive datetime is rejected on the way in
rather than assumed to be UTC: an assumption about a timezone is how a session
appears to end before it began.

## Enums

Stored as their `.value`, decoded by looking the value up in the enum class. An
unrecognised value raises rather than falling back — a status this code does
not understand must not be treated as one it does.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Type

from ..audit.events import AuditAction, AuditEvent, AuditResult
from ..domain.adaptation import (
    AdaptationOrigin,
    AdaptationRecord,
    NormalizedSignal,
    SignalKind,
    SignalSource,
)
from ..domain.alignment import (
    ActivityGoalAlignment,
    AlignmentRole,
    AlignmentSource,
    CapacityLedger,
    CoverageGap,
    CoverageGapReason,
)
from ..domain.auth_identity import AuthSubjectIdentityClaim
from ..domain.child_context import ChildContextRecord
from ..domain.intervention import (
    InterventionAction,
    InterventionScope,
    TherapistIntervention,
)
from ..domain.month_end import (
    MonthEndReport,
    ReportSection,
    ReportState,
    SectionContent,
)
from ..domain.rtm import (
    EpisodeStatus,
    PeriodStatus,
    RegulatoryStatus,
    RTMEpisode,
    RTMMonitoringPeriod,
    RTMTechnology,
)
from ..domain.rtm_documentation import (
    ClinicalAction,
    ClinicalActionType,
    InteractionModality,
    ParticipantType,
    SynchronousInteraction,
    TherapistReview,
    TimeEntry,
    TimeEntryMethod,
)
from ..domain.rtm_summary import (
    CodingAssistanceSummary,
    GoalEvidenceLine,
    GoalStatusRecommendation,
    RTMEvidenceSummary,
)
from ..coding.rules import ConfirmationStatus, MissingRequirement
from ..domain.observation import (
    AttemptOutcome,
    CustomizationSignalType,
    DeferRecord,
    Difficulty,
    Enjoyment,
    ObservationEvent,
    ParentCustomizationSignal,
    TimezoneSource,
)
from ..domain.weekly_cycle import (
    GenerationReason,
    PartialReason,
    WeeklyCycle,
    WeeklyPlanLink,
    WeeklyPlanSnapshot,
)
from ..domain.canonical_rung import ActivityFamilyBinding, CanonicalRung
from ..domain.parent_baseline_projection import ParentBaselineProjection
from ..domain.parent_session_claim import ParentSessionClaim
from ..domain.suggestion_generation import (
    GoalSuggestionGenerationClaim,
)
from ..domain.goal_anchor import ClinicalGoalAnchor, SuggestionCanonicalAnchor
from ..domain.goals import (
    CaregiverApprovedGoal,
    ClinicalGoal,
    EditType,
    EvidenceSource,
    GoalKind,
    GoalStatus,
    GoalSuggestion,
    GoalSuggestionEvidence,
    GoalVersion,
    SuggestionStatus,
)
from ..domain.identity_claims import ClaimKind, ClaimRecordKind, IdentityClaim
from ..domain.monthly_plan import (
    AllocationStatus,
    MonthlyFocusPlan,
    MonthlyGoalAllocation,
    MonthlyGoalSnapshot,
    MonthlyPlanState,
)
from ..domain.managing_clinician import (
    ManagingClinicianAssignment,
    ManagingClinicianStatus,
)
from ..domain.source_link import SourceLinkStatus, SourceSystem, SourceSystemLink
from ..domain.connections import CaregiverChildConnection, ProviderChildConnection
from ..domain.entities import Caregiver, Child, Practice, Provider
from ..domain.enums import (
    CaregiverRelationship,
    ConnectionInitiator,
    ConnectionStatus,
    EntityStatus,
    ProviderDiscipline,
)
from ..domain.roles import ActorRole
from ..revision.records import RecordState, Revision


class CodecError(ValueError):
    """A document could not be encoded or decoded.

    PHI-safe: names the type and the offending FIELD, never the field's value.
    """

    PHI_SAFE_MESSAGE = True


# -- field kinds -----------------------------------------------------------

class Kind:
    """Base for the explicit per-field handling rules."""

    optional = False

    def to_doc(self, value: Any, field: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def from_doc(self, value: Any, field: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


class _Str(Kind):
    def __init__(self, optional: bool = False) -> None:
        self.optional = optional

    def to_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required string is None")
            return None
        if not isinstance(value, str):
            raise CodecError(f"{field}: expected a string")
        return value

    def from_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required string is null")
            return None
        if not isinstance(value, str):
            raise CodecError(f"{field}: expected a string")
        return value


class _Int(Kind):
    def to_doc(self, value: Any, field: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CodecError(f"{field}: expected an integer")
        return value

    def from_doc(self, value: Any, field: str) -> Any:
        return self.to_doc(value, field)


class _DateTime(Kind):
    def __init__(self, optional: bool = False) -> None:
        self.optional = optional

    def to_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required timestamp is None")
            return None
        if not isinstance(value, datetime):
            raise CodecError(f"{field}: expected a datetime")
        if value.tzinfo is None:
            raise CodecError(f"{field}: refusing to store a naive datetime")
        return value.astimezone(timezone.utc).isoformat()

    def from_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required timestamp is null")
            return None
        if not isinstance(value, str):
            raise CodecError(f"{field}: expected an ISO-8601 string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise CodecError(f"{field}: not a valid ISO-8601 timestamp")
        if parsed.tzinfo is None:
            raise CodecError(f"{field}: stored timestamp has no timezone")
        return parsed.astimezone(timezone.utc)


class _EnumKind(Kind):
    def __init__(self, enum_cls: Type[Enum], optional: bool = False) -> None:
        self.enum_cls = enum_cls
        self.optional = optional

    def to_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required enum is None")
            return None
        if not isinstance(value, self.enum_cls):
            raise CodecError(f"{field}: expected {self.enum_cls.__name__}")
        return value.value

    def from_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required enum is null")
            return None
        try:
            return self.enum_cls(value)
        except ValueError:
            raise CodecError(f"{field}: unrecognised {self.enum_cls.__name__} value")


class _StrMap(Kind):
    def to_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, Mapping):
            raise CodecError(f"{field}: expected a mapping")
        out = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise CodecError(f"{field}: mapping must be string to string")
            out[key] = item
        return out

    def from_doc(self, value: Any, field: str) -> Any:
        return self.to_doc(value, field)


class _Bool(Kind):
    def to_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, bool):
            raise CodecError(f"{field}: expected a boolean")
        return value

    def from_doc(self, value: Any, field: str) -> Any:
        return self.to_doc(value, field)


class _StrTuple(Kind):
    """An ORDERED list of strings, round-tripped as a tuple.

    Order is preserved rather than sorted: a clinician's routine list and a
    suggestion's milestone refs both carry meaning in their sequence, and a
    codec that quietly reorders them would make two records that differ look
    identical — and two that are identical fail an equality test.
    """

    def to_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of strings")
        for item in value:
            if not isinstance(item, str):
                raise CodecError(f"{field}: sequence must contain only strings")
        return list(value)

    def from_doc(self, value: Any, field: str) -> Any:
        return tuple(self.to_doc(value, field))


class _Nested(Kind):
    """An embedded record with its own registered spec.

    Embedded rather than referenced because `GoalSuggestionEvidence` has no
    independent lifetime: it is never queried, never updated, and meaningless
    apart from its suggestion. It inherits the same exact-key-set strictness,
    so a field added to the nested type cannot half-land either.
    """

    def __init__(self, cls: type, optional: bool = False) -> None:
        self.cls = cls
        self.optional = optional

    def to_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required nested record is None")
            return None
        if not isinstance(value, self.cls):
            raise CodecError(f"{field}: expected {self.cls.__name__}")
        return encode(value)

    def from_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required nested record is null")
            return None
        if not isinstance(value, Mapping):
            raise CodecError(f"{field}: expected a nested document")
        return decode(self.cls, value)


class _EnumTuple(Kind):
    """An ORDERED list of enum members, round-tripped as a tuple."""

    def __init__(self, enum_cls: Type[Enum]) -> None:
        self.enum_cls = enum_cls

    def to_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of enums")
        out = []
        for item in value:
            if not isinstance(item, self.enum_cls):
                raise CodecError(f"{field}: expected {self.enum_cls.__name__}")
            out.append(item.value)
        return out

    def from_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of enum values")
        try:
            return tuple(self.enum_cls(item) for item in value)
        except ValueError:
            raise CodecError(
                f"{field}: unrecognised {self.enum_cls.__name__} value") from None


class _NestedTuple(Kind):
    """An ORDERED list of embedded records with their own registered spec.

    Embedded rather than referenced for the same reason as
    `GoalSuggestionEvidence`: a `NormalizedSignal` has no independent
    lifetime and is meaningless apart from the adaptation it explains. Each
    element inherits the exact-key-set strictness, so a field added to the
    nested type cannot half-land either.
    """

    def __init__(self, cls: type) -> None:
        self.cls = cls

    def to_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of records")
        out = []
        for item in value:
            if not isinstance(item, self.cls):
                raise CodecError(f"{field}: expected {self.cls.__name__}")
            out.append(encode(item))
        return out

    def from_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of documents")
        return tuple(decode(self.cls, item) for item in value)


class _OptInt(Kind):
    """An integer that may genuinely be absent.

    Distinct from defaulting to zero. A synchronous interaction with no
    recorded duration is NOT a zero-minute interaction — the duration was
    never captured, and zero would be a measurement nobody made.
    """

    optional = True

    def to_doc(self, value: Any, field: str) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise CodecError(f"{field}: expected an integer or null")
        return value

    def from_doc(self, value: Any, field: str) -> Any:
        return self.to_doc(value, field)


class _PairTuple(Kind):
    """An ORDERED list of (str, str|int) pairs, stored as a list of MAPS.

    Used for goal references, report counts and code candidates. A list
    rather than a mapping because ORDER carries meaning — report sections
    and candidate lists are read in sequence — and because a mapping would
    silently drop a duplicate key where a list keeps both.

    Each pair is a `{"key": ..., "value": ...}` map and NOT a two-element
    list, because **Firestore does not support nested arrays**. The first
    implementation used `[[k, v], ...]`; `FakeDocumentStore` is a plain dict
    and stored it happily, so every unit test passed, and the real client
    rejected the write. It affected seven of the eleven 0.4F/G record types,
    none of which could have persisted to a real database.

    The emulator suite exists for exactly this: a shape the fake accepts and
    the real store refuses.
    """

    KEY, VALUE = "key", "value"

    def __init__(self, value_type: type) -> None:
        self.value_type = value_type

    def to_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of pairs")
        out = []
        for item in value:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise CodecError(f"{field}: each entry must be a pair")
            key, val = item
            if not isinstance(key, str):
                raise CodecError(f"{field}: pair keys must be strings")
            if (isinstance(val, bool)
                    or not isinstance(val, self.value_type)):
                raise CodecError(
                    f"{field}: pair values must be {self.value_type.__name__}")
            out.append({self.KEY: key, self.VALUE: val})
        return out

    def from_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of pairs")
        pairs = []
        for item in value:
            if not isinstance(item, Mapping) or set(item) != {self.KEY,
                                                              self.VALUE}:
                raise CodecError(
                    f"{field}: each entry must be a key/value map")
            pairs.append((item[self.KEY], item[self.VALUE]))
        return tuple(pairs)


class _EnumTuple(Kind):
    """An ORDERED list of enum members, stored as their values.

    Order is preserved: the coding rules emit missing-requirement flags in a
    deliberate sequence, and a codec that sorted them would make two
    different evaluations compare equal.
    """

    def __init__(self, enum_cls: Type[Enum]) -> None:
        self.enum_cls = enum_cls

    def to_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of enum members")
        out = []
        for item in value:
            if not isinstance(item, self.enum_cls):
                raise CodecError(
                    f"{field}: expected {self.enum_cls.__name__} members")
            out.append(item.value)
        return out

    def from_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, (tuple, list)):
            raise CodecError(f"{field}: expected a sequence of values")
        try:
            return tuple(self.enum_cls(item) for item in value)
        except ValueError:
            raise CodecError(
                f"{field}: unrecognised {self.enum_cls.__name__} value") from None


STR, OPT_STR = _Str(), _Str(optional=True)
INT = _Int()
OPT_INT = _OptInt()
BOOL = _Bool()
DT, OPT_DT = _DateTime(), _DateTime(optional=True)
STR_MAP = _StrMap()
STR_TUPLE = _StrTuple()
STR_PAIR_TUPLE = _PairTuple(str)
INT_PAIR_TUPLE = _PairTuple(int)


#: Per-type field handling. A test asserts each spec covers exactly the
#: dataclass's fields, so a new field cannot be added without a decision here.
SPECS: Dict[type, Dict[str, Kind]] = {
    Practice: {
        "practice_id": STR, "legal_name": STR,
        "status": _EnumKind(EntityStatus),
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    Provider: {
        "provider_id": STR, "practice_id": STR,
        "discipline": _EnumKind(ProviderDiscipline),
        "display_name": STR, "auth_subject": OPT_STR,
        "status": _EnumKind(EntityStatus),
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    Caregiver: {
        "caregiver_id": STR, "display_name": STR, "auth_subject": OPT_STR,
        "status": _EnumKind(EntityStatus),
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    Child: {
        "child_id": STR,
        "status": _EnumKind(EntityStatus),
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    CaregiverChildConnection: {
        "connection_id": STR, "caregiver_id": STR, "child_id": STR,
        "relationship_role": _EnumKind(CaregiverRelationship),
        "status": _EnumKind(ConnectionStatus),
        "created_at": DT, "updated_at": DT, "ended_at": OPT_DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    ProviderChildConnection: {
        "connection_id": STR, "provider_id": STR, "child_id": STR,
        "practice_id": STR,
        "status": _EnumKind(ConnectionStatus),
        "permissions": STR,
        "created_at": DT, "updated_at": DT,
        "activated_at": OPT_DT, "ended_at": OPT_DT,
        "created_by_actor_id": OPT_STR,
        # 0.5B. Both carry the dataclass default when absent from a stored
        # document, which is what makes the field addition backward
        # compatible: pre-0.5B rows decode to CAREGIVER (the only initiator
        # that existed) and to `paused_at=None` (pausing did not exist).
        "initiated_by": _EnumKind(ConnectionInitiator),
        "paused_at": OPT_DT,
        "schema_version": STR,
    },
    AuditEvent: {
        "event_id": STR, "occurred_at": DT,
        "action": _EnumKind(AuditAction), "result": _EnumKind(AuditResult),
        "resource_type": STR, "resource_id": OPT_STR, "child_id": OPT_STR,
        "actor_application_id": OPT_STR, "actor_auth_subject": OPT_STR,
        "actor_role": _EnumKind(ActorRole, optional=True),
        "request_id": STR, "metadata": STR_MAP, "schema_version": STR,
    },
    ChildContextRecord: {
        "record_id": STR, "child_id": STR, "content_ref": STR,
        "current_revision_id": OPT_STR, "current_version": INT,
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR,
        "last_actor_role": _EnumKind(ActorRole, optional=True),
        "schema_version": STR,
    },
    SourceSystemLink: {
        "link_id": STR, "child_id": STR,
        "source_system": _EnumKind(SourceSystem),
        "external_id": STR, "external_owner_ref": STR,
        "status": _EnumKind(SourceLinkStatus),
        "linked_at": DT, "linked_by_actor_id": OPT_STR, "linked_by_role": OPT_STR,
        "ended_at": OPT_DT, "ended_by_actor_id": OPT_STR, "end_reason": STR,
        "child_source_claim_id": STR, "external_identity_claim_id": STR,
        "superseded_by_link_id": OPT_STR, "supersedes_link_id": OPT_STR,
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    ManagingClinicianAssignment: {
        "assignment_id": STR, "child_id": STR, "provider_id": STR,
        "practice_id": STR, "provider_connection_id": STR,
        "status": _EnumKind(ManagingClinicianStatus),
        "effective_from": DT, "effective_to": OPT_DT,
        "assigned_by_actor_id": OPT_STR, "assigned_by_role": OPT_STR,
        "reason": STR, "end_reason": STR, "ended_by_actor_id": OPT_STR,
        "claim_id": STR,
        "supersedes_assignment_id": OPT_STR,
        "superseded_by_assignment_id": OPT_STR,
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    AuthSubjectIdentityClaim: {
        "claim_id": STR, "subject_fingerprint": STR,
        "holder_actor_id": STR,
        "holder_actor_type": _EnumKind(ActorRole),
        "created_at": DT, "schema_version": STR,
    },
    IdentityClaim: {
        "claim_id": STR, "record_kind": _EnumKind(ClaimRecordKind),
        "kind": _EnumKind(ClaimKind), "key_digest": STR,
        "generation": INT, "holder_ref": STR, "child_id": STR,
        "created_at": DT, "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    # ---- 0.5E-A canonical anchors --------------------------------------
    #
    # `is_activity_mappable` is ABSENT from every spec below on purpose. It is
    # a derived property, so there is no stored copy that could drift away
    # from the rung it summarises. The codec drives off dataclass fields, and
    # a property is not a field — the omission is structural, not a choice
    # someone has to keep making.
    ActivityFamilyBinding: {
        "family_ref": STR, "allowed_domains": STR_TUPLE,
    },
    CanonicalRung: {
        "domain_key": STR, "source_rung_months": INT,
        "milestone_text": STR, "subdomain": STR,
        "family_bindings": _NestedTuple(ActivityFamilyBinding),
        "track_subdomains": STR_TUPLE, "track_families": STR_TUPLE,
        "rung_ref": STR, "track_ref": STR,
        "taxonomy_version": STR, "baseline_version": STR,
    },
    # ---- 0.5F-A2 Parent baseline projection ----------------------------
    #
    # `has_routing_anchor` is ABSENT: it is a derived property, so there is no
    # stored copy that could claim a planning anchor the record does not have.
    # Flattened rather than nesting the seven baseline fields, so the stored
    # document is directly queryable and this spec stays readable.
    ParentBaselineProjection: {
        "projection_id": STR, "child_id": STR,
        "source_system": _EnumKind(SourceSystem),
        "source_session_id": STR, "source_record_digest": STR,
        "domain": STR, "area_id": STR, "entry_choice_id": STR,
        "status": STR, "baseline_version": STR,
        "routing_anchor_months": OPT_INT,
        "not_demonstrated_months": OPT_INT,
        "projected_at": DT, "schema_version": STR,
    },
    # ---- 0.5F-A3 Parent session handoff claim ---------------------------
    #
    # SIX fields and not one more. There is no `consumed` flag: redemption is a
    # separate create-only identity claim, so this record is written once and
    # never edited. There is no uid of either system, and no clinical field.
    ParentSessionClaim: {
        "claim_digest": STR,
        "source_system": _EnumKind(SourceSystem),
        "source_session_id": STR,
        "issued_at": DT, "expires_at": DT,
        "schema_version": STR,
    },
    # ---- 0.5F-B deterministic generation claim -------------------------
    #
    # `suggestion_ids` is the lineage: projection -> claim -> suggestions. The
    # requester is stored for AUDIT only and is deliberately not part of the
    # generation key, so two authorized providers converge on one claim.
    GoalSuggestionGenerationClaim: {
        "claim_id": STR, "generation_key": STR,
        "projection_id": STR, "child_id": STR, "domain_key": STR,
        "target_rung_ref": STR, "target_rung_months": INT,
        "generation_policy": STR, "taxonomy_version": STR,
        "gold_standard_version": STR,
        "suggestion_ids": STR_TUPLE,
        "created_at": DT, "requested_by_actor_id": OPT_STR,
        "schema_version": STR,
    },
    SuggestionCanonicalAnchor: {
        "suggestion_id": STR, "child_id": STR,
        "rung": _Nested(CanonicalRung),
        "created_at": DT, "schema_version": STR,
    },
    ClinicalGoalAnchor: {
        "clinical_goal_id": STR, "child_id": STR,
        "source_suggestion_id": STR,
        "rung": _Nested(CanonicalRung),
        "created_at": DT, "schema_version": STR,
    },
    GoalSuggestionEvidence: {
        "domain_key": STR, "evidence_source": _EnumKind(EvidenceSource),
        "milestone_refs": STR_TUPLE, "functional_baseline_area": STR,
        "observed_level": STR, "explicitly_selected": BOOL,
        "prior_month_summary_id": OPT_STR, "rule_version": STR,
    },
    GoalSuggestion: {
        "suggestion_id": STR, "child_id": STR, "cycle_month": STR,
        "family_facing_text_template": STR,
        "evidence": _Nested(GoalSuggestionEvidence),
        "suggested_priority_rank": INT, "suggested_emphasis_weight": INT,
        "generator_version": STR, "generation_mode": STR,
        "status": _EnumKind(SuggestionStatus),
        "created_at": DT, "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    GoalVersion: {
        "version_id": STR, "goal_kind": _EnumKind(GoalKind), "goal_id": STR,
        "version_number": INT, "text": STR,
        "edit_type": _EnumKind(EditType),
        "actor_id": STR, "actor_role": _EnumKind(ActorRole),
        "derived_from_suggestion_id": OPT_STR, "reason": STR,
        "supersedes_version_id": OPT_STR,
        "created_at": DT, "schema_version": STR,
    },
    ClinicalGoal: {
        "clinical_goal_id": STR, "child_id": STR, "managing_provider_id": STR,
        "practice_id": STR, "managing_assignment_id": STR,
        "current_version_id": STR, "status": _EnumKind(GoalStatus),
        "opened_at": DT, "closed_at": OPT_DT,
        "created_by_actor_id": OPT_STR, "created_at": DT, "updated_at": DT,
        "schema_version": STR,
    },
    CaregiverApprovedGoal: {
        "caregiver_goal_id": STR, "child_id": STR,
        "approved_by_caregiver_id": STR, "current_version_id": STR,
        "status": _EnumKind(GoalStatus),
        "opened_at": DT, "closed_at": OPT_DT,
        "created_by_actor_id": OPT_STR, "created_at": DT, "updated_at": DT,
        "schema_version": STR,
    },
    MonthlyFocusPlan: {
        "focus_plan_id": STR, "child_id": STR, "cycle_month": STR,
        "timezone_of_record": STR, "starts_on": STR, "ends_on": STR,
        "monitoring_focus": STR, "routines_context": STR_TUPLE,
        "interests_motivators": STR_TUPLE, "support_considerations": STR,
        "expected_practice_cadence": STR, "monitoring_dimensions": STR_TUPLE,
        "clinician_guidance": STR,
        "state": _EnumKind(MonthlyPlanState), "policy_version": STR,
        "current_revision_id": OPT_STR,
        "activated_at": OPT_DT, "closed_at": OPT_DT,
        "activation_claim_id": STR,
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR,
        "created_by_role": _EnumKind(ActorRole, optional=True),
        "schema_version": STR,
    },
    MonthlyGoalAllocation: {
        "allocation_id": STR, "focus_plan_id": STR, "child_id": STR,
        "goal_kind": _EnumKind(GoalKind), "goal_id": STR,
        "priority_rank": INT, "emphasis_weight": INT,
        "min_coverage_per_cycle": INT,
        "status": _EnumKind(AllocationStatus),
        "set_by_actor_id": OPT_STR,
        "set_by_role": _EnumKind(ActorRole, optional=True),
        "effective_from_cycle": INT,
        "supersedes_allocation_id": OPT_STR,
        "superseded_by_allocation_id": OPT_STR,
        "reason": STR, "created_at": DT, "updated_at": DT,
        "schema_version": STR,
    },
    MonthlyGoalSnapshot: {
        "snapshot_id": STR, "focus_plan_id": STR, "child_id": STR,
        "goal_kind": _EnumKind(GoalKind), "goal_id": STR,
        "goal_version_id": STR, "goal_text_at_snapshot": STR,
        "priority_rank": INT, "emphasis_weight": INT,
        "min_coverage_per_cycle": INT,
        "approved_by_role": _EnumKind(ActorRole),
        "allocation_id": STR,
        "effective_from": DT, "snapshot_at": DT,
        "schema_version": STR,
    },
    WeeklyCycle: {
        "cycle_id": STR, "owning_focus_plan_id": STR, "child_id": STR,
        "sequence_in_month": INT, "starts_on": STR, "ends_on": STR,
        "is_partial": BOOL,
        "partial_reason": _EnumKind(PartialReason, optional=True),
        "spans_month_boundary": BOOL,
        "predecessor_cycle_id": OPT_STR,
        "generation_reason": _EnumKind(GenerationReason),
        "engine_version": STR,
        "released_to_parent_at": OPT_DT,
        "adaptation_record_id": OPT_STR,
        "created_at": DT, "updated_at": DT, "schema_version": STR,
    },
    WeeklyPlanLink: {
        "link_id": STR, "cycle_id": STR,
        "source_system": _EnumKind(SourceSystem),
        "external_plan_id": STR, "linked_at": DT,
        "coverage_local_dates": STR_TUPLE, "schema_version": STR,
    },
    WeeklyPlanSnapshot: {
        "snapshot_id": STR, "cycle_id": STR, "resolved_plan_document": STR,
        "source_system": _EnumKind(SourceSystem), "source_plan_id": STR,
        "source_generated_at": OPT_DT, "captured_at": DT,
        "schema_version": STR,
    },
    ActivityGoalAlignment: {
        "alignment_id": STR, "cycle_id": STR, "child_id": STR,
        "activity_instance_ref": STR, "activity_identity_ref": STR,
        "goal_kind": _EnumKind(GoalKind), "goal_id": STR,
        "role": _EnumKind(AlignmentRole),
        "alignment_source": _EnumKind(AlignmentSource),
        "rationale": STR, "milestone_refs": STR_TUPLE, "rule_version": STR,
        "allocation_id": OPT_STR, "assigned_by_actor_id": OPT_STR,
        "created_at": DT, "schema_version": STR,
    },
    CoverageGap: {
        "gap_id": STR, "cycle_id": STR, "child_id": STR,
        "goal_kind": _EnumKind(GoalKind), "goal_id": STR,
        "reason": _EnumKind(CoverageGapReason),
        "capacity_available": INT, "capacity_required": INT,
        "rule_version": STR, "detail": STR,
        "created_at": DT, "schema_version": STR,
    },
    CapacityLedger: {
        "ledger_id": STR, "cycle_id": STR, "child_id": STR,
        "family_declared_capacity": INT, "allocated_by_planner": INT,
        "clinician_added": INT, "overage_reason": STR,
        "created_at": DT, "updated_at": DT, "schema_version": STR,
    },
    ObservationEvent: {
        "event_id": STR, "child_id": STR, "owning_cycle_id": STR,
        "attribution_month": STR, "local_date": STR, "occurred_at": DT,
        "timezone_of_record": STR, "tz_source": _EnumKind(TimezoneSource),
        "activity_instance_ref": STR,
        "attempt_outcome": _EnumKind(AttemptOutcome),
        "difficulty": _EnumKind(Difficulty, optional=True),
        "enjoyment": _EnumKind(Enjoyment, optional=True),
        "assistance": STR, "child_response": STR,
        "observation_text_ref": STR, "source_feedback_id": STR,
        "recorded_by_caregiver_id": STR,
        "created_at": DT, "schema_version": STR,
    },
    ParentCustomizationSignal: {
        "signal_id": STR, "cycle_id": STR, "child_id": STR,
        "activity_instance_ref": STR,
        "signal_type": _EnumKind(CustomizationSignalType),
        "actor_id": STR, "source_overlay_ref": STR,
        "created_at": DT, "schema_version": STR,
    },
    DeferRecord: {
        "defer_id": STR, "child_id": STR, "activity_instance_ref": STR,
        "activity_identity_ref": STR, "deferred_by_actor_id": STR,
        "deferred_by_role": _EnumKind(ActorRole),
        "from_cycle_id": STR, "from_cycle_sequence": INT,
        "suppression_until_cycle": INT, "created_at": DT,
        "became_eligible_at": OPT_DT, "override_reason": STR,
        "overridden_by_actor_id": OPT_STR, "schema_version": STR,
    },
    TherapistIntervention: {
        "intervention_id": STR, "child_id": STR, "cycle_id": STR,
        "provider_id": STR, "action": _EnumKind(InterventionAction),
        "applies_to": _EnumKind(InterventionScope),
        "clinical_rationale": STR, "target_ref": STR, "guidance_text": STR,
        "managing_assignment_id": STR,
        "created_at": DT, "schema_version": STR,
    },
    NormalizedSignal: {
        "kind": _EnumKind(SignalKind), "source": _EnumKind(SignalSource),
        "source_ref": STR, "activity_identity_ref": STR, "goal_ref_key": STR,
    },
    AdaptationRecord: {
        "record_id": STR, "child_id": STR, "focus_plan_id": STR,
        "from_cycle_id": STR, "to_cycle_id": STR,
        "evidence_event_ids": STR_TUPLE,
        "customization_signal_ids": STR_TUPLE,
        "defer_record_ids": STR_TUPLE,
        "normalized_signals": _NestedTuple(NormalizedSignal),
        "rule_version": STR, "origin": _EnumKind(AdaptationOrigin),
        "signal_source": _EnumTuple(SignalSource),
        "clinician_decision": STR, "source_action_ref": STR,
        "intervention_id": OPT_STR,
        "goal_alignment_before": STR_TUPLE,
        "goal_alignment_after": STR_TUPLE,
        "coverage_gaps": STR_TUPLE,
        "not_a_failure": BOOL, "resulting_change": STR,
        "created_at": DT, "schema_version": STR,
    },
    RTMEpisode: {
        "episode_id": STR, "child_id": STR, "managing_provider_id": STR,
        "practice_id": STR, "clinical_goal_refs": STR_PAIR_TUPLE,
        "opened_at": DT, "opened_by_actor_id": STR,
        "managing_assignment_id": STR,
        "status": _EnumKind(EpisodeStatus),
        "closed_at": OPT_DT, "closed_by_actor_id": OPT_STR,
        "close_reason": STR,
        "created_at": DT, "updated_at": DT, "schema_version": STR,
    },
    RTMMonitoringPeriod: {
        "period_id": STR, "episode_id": STR, "child_id": STR,
        "focus_plan_id": STR, "cycle_month": STR, "timezone_of_record": STR,
        "status": _EnumKind(PeriodStatus),
        "started_at": DT, "activated_at": OPT_DT,
        "finalized_at": OPT_DT, "finalized_by_actor_id": OPT_STR,
        "uniqueness_claim_id": STR,
        "created_at": DT, "updated_at": DT, "schema_version": STR,
    },
    RTMTechnology: {
        "technology_id": STR, "episode_id": STR, "child_id": STR,
        "product_descriptor": STR,
        "regulatory_status": _EnumKind(RegulatoryStatus),
        "technology_version": STR, "attestation_ref": STR,
        "created_at": DT, "updated_at": DT,
        "supersedes_technology_id": OPT_STR,
        "superseded_by_technology_id": OPT_STR,
        "schema_version": STR,
    },
    TherapistReview: {
        "review_id": STR, "period_id": STR, "child_id": STR,
        "provider_id": STR, "clinical_interpretation": STR,
        "reviewed_event_ids": STR_TUPLE, "reviewed_cycle_ids": STR_TUPLE,
        "created_at": DT, "schema_version": STR,
    },
    ClinicalAction: {
        "action_id": STR, "review_id": STR, "period_id": STR,
        "child_id": STR, "provider_id": STR,
        "action_type": _EnumKind(ClinicalActionType),
        "narrative": STR, "created_at": DT, "schema_version": STR,
    },
    TimeEntry: {
        "time_entry_id": STR, "period_id": STR, "child_id": STR,
        "provider_id": STR, "local_date": STR, "timezone_of_record": STR,
        "minutes": INT, "activity_description": STR, "entered_at": DT,
        "entry_method": _EnumKind(TimeEntryMethod),
        "source_review_id": OPT_STR, "source_action_id": OPT_STR,
        "supersedes_time_entry_id": OPT_STR,
        "superseded_by_time_entry_id": OPT_STR,
        "correction_reason": STR, "schema_version": STR,
    },
    SynchronousInteraction: {
        "interaction_id": STR, "period_id": STR, "child_id": STR,
        "provider_id": STR, "occurred_at_utc": DT, "local_date": STR,
        "timezone_of_record": STR,
        "modality": _EnumKind(InteractionModality),
        "participant_type": _EnumKind(ParticipantType),
        "duration_minutes": OPT_INT, "real_time_affirmed": BOOL,
        "note_ref": STR, "entered_at": DT, "schema_version": STR,
    },
    GoalEvidenceLine: {
        "goal_kind": STR, "goal_id": STR, "attributed_attempts": INT,
        "attributed_completions": INT, "scheduled_opportunities": INT,
        "status_recommendation": _EnumKind(GoalStatusRecommendation),
    },
    RTMEvidenceSummary: {
        "summary_id": STR, "period_id": STR, "child_id": STR,
        "generated_at": DT, "rule_version": STR, "focus_plan_ref": STR,
        "clinical_goal_refs": STR_PAIR_TUPLE, "cycle_refs": STR_TUPLE,
        "total_distinct_observation_events": INT,
        "distinct_observed_local_dates": INT,
        "did_it_count": INT, "wasnt_ready_yet_count": INT,
        "didnt_want_to_try_count": INT,
        "per_goal_evidence": _NestedTuple(GoalEvidenceLine),
        "multi_goal_overlap_count": INT,
        "therapist_review_count": INT, "clinical_action_count": INT,
        "documented_management_minutes": INT,
        "synchronous_interaction_count": INT,
        "real_time_interactive_communication_present": BOOL,
        "documentation_missing_flags": STR_TUPLE,
        "technology_regulatory_status": _EnumKind(RegulatoryStatus),
        "schema_version": STR,
    },
    CodingAssistanceSummary: {
        "coding_summary_id": STR, "period_id": STR, "child_id": STR,
        "generated_at": DT, "coding_rule_set_id": STR,
        "coding_rule_version": STR,
        "documented_management_minutes": INT,
        "real_time_interactive_communication_present": BOOL,
        "synchronous_interaction_refs": STR_TUPLE,
        "time_entry_refs": STR_TUPLE,
        "potential_code_candidates": INT_PAIR_TUPLE,
        "rule_explanations": STR_TUPLE,
        "missing_requirement_flags": _EnumTuple(MissingRequirement),
        "technology_regulatory_status": _EnumKind(RegulatoryStatus),
        "clinician_confirmation_status": _EnumKind(ConfirmationStatus),
        "clinician_confirmed_by": OPT_STR,
        "clinician_confirmed_at": OPT_DT,
        "confirmation_note": STR, "schema_version": STR,
    },
    SectionContent: {
        "section": _EnumKind(ReportSection),
        "record_refs": STR_TUPLE, "counts": INT_PAIR_TUPLE,
        "labels": STR_PAIR_TUPLE, "attributed_to_role": STR,
        "overlap_declared": BOOL,
    },
    MonthEndReport: {
        "report_id": STR, "period_id": STR, "child_id": STR,
        "focus_plan_id": STR, "cycle_month": STR,
        "sections": _NestedTuple(SectionContent),
        "state": _EnumKind(ReportState), "version": INT,
        "generated_at": DT, "finalized_at": OPT_DT,
        "finalized_by_actor_id": OPT_STR,
        "supersedes_report_id": OPT_STR, "superseded_by_report_id": OPT_STR,
        "amendment_reason": STR, "amended_by_actor_id": OPT_STR,
        "evidence_summary_id": STR, "coding_summary_id": STR,
        "created_at": DT, "updated_at": DT, "schema_version": STR,
    },
    Revision: {
        "revision_id": STR, "record_id": STR, "version": INT,
        "state": _EnumKind(RecordState), "created_at": DT,
        "actor_application_id": STR, "actor_role": _EnumKind(ActorRole),
        "content_ref": STR, "supersedes_revision_id": OPT_STR,
        "amendment_reason": STR, "finalized_at": OPT_DT,
        "schema_version": STR,
    },
}


def _spec_for(cls: type) -> Dict[str, Kind]:
    try:
        return SPECS[cls]
    except KeyError:
        raise CodecError(f"no codec registered for {cls.__name__}")


def encode(record: Any) -> Dict[str, Any]:
    """Serialize a record to a plain document.

    Drives off the dataclass's own fields, so completeness is structural: a
    field present on the model is always written.
    """
    spec = _spec_for(type(record))
    document: Dict[str, Any] = {}
    for f in dataclass_fields(record):
        kind = spec.get(f.name)
        if kind is None:
            raise CodecError(f"{type(record).__name__}.{f.name} has no codec rule")
        document[f.name] = kind.to_doc(getattr(record, f.name), f.name)
    return document


def decode(cls: type, document: Mapping[str, Any]) -> Any:
    """Deserialize a document, refusing any key-set mismatch."""
    spec = _spec_for(cls)
    expected = {f.name for f in dataclass_fields(cls)}
    present = set(document)

    missing = sorted(expected - present)
    unknown = sorted(present - expected)
    if missing:
        raise CodecError(f"{cls.__name__}: document is missing fields: {', '.join(missing)}")
    if unknown:
        raise CodecError(f"{cls.__name__}: document has unknown fields: {', '.join(unknown)}")

    kwargs = {name: spec[name].from_doc(document[name], name) for name in expected}
    return cls(**kwargs)


def registered_types() -> Dict[str, type]:
    """Diagnostics/tests: every type this module can round-trip."""
    return {cls.__name__: cls for cls in SPECS}
