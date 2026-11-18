"""pilot_backend/weekly/adaptation.py — deterministic Week N to Week N+1.

No model, no prompt, no network. Same inputs, same decisions, every time —
guarded by the same AST import scan that guards the 0.4B/C suggestion engine.

## Normalization keeps the source attached

Every input becomes a `NormalizedSignal` carrying its `SignalSource` and the
id of the row it came from. A conclusion can always be traced back to a
record, and a clinician decision can never be read as something the child did
because the two use different `SignalKind` namespaces.

## The direction table — SUPERSEDED by P0-B1 (rule version 2026.11-p0b)

The 2026.10 table read ONE caregiver observation as a difficulty direction:

    too_hard | wasnt_ready_yet | didnt_want_to_try  -> EASIER_OR_MORE_SUPPORT
    too_easy AND did_it                             -> HARDER_OR_PROGRESSED

Founder decision (P0-B1) retired it, because it contradicts the approved policy
three ways: a single `wasnt_ready_yet` must not adapt anything; disengagement
(`didnt_want_to_try`) must not be read as inability; and `too_easy` must never
imply progression. Caregiver evidence and family plan edits therefore set NO
difficulty direction here any more — every observed identity is `MAINTAIN` on
the difficulty axis. What a family's feedback CAN change is decided by
`repeated_signal_policy` (theme / reviewed variation only) and enforced by the
release composition. `normalize()` is unchanged: classification is provenance,
not a decision.

Only CURRENT observations are read: `current_observations()` is applied before
anything else, so a superseded submission is never evidence.

A clinician's FUTURE_CYCLE intervention still sets its direction below. That is
a clinical decision by the managing clinician, not an autonomous Genex change.

Unmappable feedback stays unmapped. Nothing invents a domain-level signal from
input the table does not cover.

## Clinician direction wins, and says so

A clinician decision sets the direction for its target and marks the record
`THERAPIST_DIRECTED`. That is not "the child struggled" — `not_a_failure`
holds on the record and on every clinician-origin signal, and the
`has_performance_evidence` property is False when the whole difference is
explained by decisions.

## Suppression is applied, never overridden

An activity under an unexpired caregiver defer gets `SUPPRESSED` and is
excluded from the next cycle's candidates. The engine has no path to release
one early; only an explicit clinician override on the `DeferRecord` can.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from ..domain.adaptation import (
    AdaptationDirection,
    AdaptationOrigin,
    NormalizedSignal,
    SignalKind,
    SignalSource,
    SUPPORT_SIGNALS,
)
from ..domain.alignment import ActivityGoalAlignment, CoverageGap
from ..domain.intervention import (
    InterventionAction,
    InterventionScope,
    TherapistIntervention,
)
from ..domain.observation import (
    AttemptOutcome,
    CustomizationSignalType,
    DeferRecord,
    Difficulty,
    ObservationEvent,
    ParentCustomizationSignal,
    current_observations,
)
from ..domain.adaptation_decision import P0B_RULE_VERSION

#: Bumped whenever normalization or the direction table changes. P0-B1 retired
#: the 2026.10 easier/harder table; see the module docstring.
ADAPTATION_RULE_VERSION = P0B_RULE_VERSION

#: The retired version, named so a regression test can assert it is gone.
SUPERSEDED_RULE_VERSION = "weekly-adaptation-rules-2026.10"

#: Caregiver outcome -> signal. `DID_IT` alone is deliberately NOT a
#: progression signal; it needs `too_easy` beside it.
_OUTCOME_SIGNALS: Mapping[AttemptOutcome, SignalKind] = {
    AttemptOutcome.DID_IT: SignalKind.CHILD_COMPLETED,
    AttemptOutcome.WASNT_READY_YET: SignalKind.CHILD_WASNT_READY,
    AttemptOutcome.DIDNT_WANT_TO_TRY: SignalKind.CHILD_DIDNT_WANT_TO_TRY,
}

_DIFFICULTY_SIGNALS: Mapping[Difficulty, SignalKind] = {
    Difficulty.TOO_HARD: SignalKind.CHILD_FOUND_IT_HARD,
    Difficulty.TOO_EASY: SignalKind.CHILD_FOUND_IT_EASY,
}

_CUSTOMIZATION_SIGNALS: Mapping[CustomizationSignalType, SignalKind] = {
    CustomizationSignalType.DEFERRED: SignalKind.PLAN_DEFERRED_BY_CAREGIVER,
    CustomizationSignalType.REMOVED_FROM_WEEK: SignalKind.PLAN_REMOVED_BY_CAREGIVER,
    CustomizationSignalType.SWAPPED: SignalKind.PLAN_SWAPPED_BY_CAREGIVER,
    CustomizationSignalType.MADE_EASIER: SignalKind.PLAN_SIMPLIFIED_BY_CAREGIVER,
    CustomizationSignalType.MADE_HARDER: SignalKind.PLAN_INTENSIFIED_BY_CAREGIVER,
    CustomizationSignalType.ADDED_BY_PARENT: SignalKind.PLAN_ADDED_BY_CAREGIVER,
    CustomizationSignalType.PARENT_DECLINED_CHANGE: SignalKind.PARENT_DECLINED_CHANGE,
}

#: Section 21. Each clinician action maps to its OWN signal so a clinical
#: decision stays distinguishable from caregiver evidence.
_INTERVENTION_SIGNALS: Mapping[InterventionAction, SignalKind] = {
    InterventionAction.ENDORSE: SignalKind.CLINICIAN_ENDORSED,
    InterventionAction.ADAPT: SignalKind.CLINICIAN_MODIFIED,
    InterventionAction.REPLACE: SignalKind.CLINICIAN_REPLACED,
    InterventionAction.ADD_GUIDANCE: SignalKind.CLINICIAN_GUIDANCE,
    InterventionAction.DEFER: SignalKind.CLINICIAN_DEFERRED,
    InterventionAction.REMOVE_FROM_SCHEDULING:
        SignalKind.CLINICIAN_REMOVED_FROM_SCHEDULING,
}

#: Clinician actions that set a direction for their target.
_INTERVENTION_DIRECTIONS: Mapping[InterventionAction, AdaptationDirection] = {
    InterventionAction.ENDORSE: AdaptationDirection.MAINTAIN,
    InterventionAction.ADAPT: AdaptationDirection.EASIER_OR_MORE_SUPPORT,
    InterventionAction.REPLACE: AdaptationDirection.MAINTAIN,
    InterventionAction.DEFER: AdaptationDirection.SUPPRESSED,
    InterventionAction.REMOVE_FROM_SCHEDULING: AdaptationDirection.SUPPRESSED,
}


class AdaptationEngineError(ValueError):
    """The engine refused its inputs. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class AdaptationPlan:
    """What the next cycle should do, and why."""

    #: activity_identity_ref -> direction. MAINTAIN is the default for
    #: anything not named here.
    directions: Mapping[str, AdaptationDirection]
    signals: Tuple[NormalizedSignal, ...]
    origin: AdaptationOrigin
    #: Identities excluded from next-cycle candidates.
    suppressed_identity_refs: Tuple[str, ...]
    #: Guidance carried forward from FUTURE_CYCLE interventions, by target.
    carried_guidance: Tuple[Tuple[str, str], ...] = ()
    rule_version: str = ADAPTATION_RULE_VERSION

    def direction_for(self, identity_ref: str) -> AdaptationDirection:
        return self.directions.get(identity_ref, AdaptationDirection.MAINTAIN)

    @property
    def has_performance_evidence(self) -> bool:
        return any(s.is_performance_signal for s in self.signals)


def _identity_index(alignments: Sequence[ActivityGoalAlignment]
                    ) -> Mapping[str, str]:
    """activity_instance_ref -> activity_identity_ref.

    Adaptation reasons about the reusable ACTIVITY, because next cycle will
    schedule a new instance. Reasoning about the instance alone would make
    every signal inapplicable the moment the week rolled over.
    """
    return {a.activity_instance_ref: a.activity_identity_ref
            for a in alignments}


def normalize(alignments: Sequence[ActivityGoalAlignment],
              events: Sequence[ObservationEvent] = (),
              customization_signals: Sequence[ParentCustomizationSignal] = (),
              defer_records: Sequence[DeferRecord] = (),
              interventions: Sequence[TherapistIntervention] = (),
              coverage_gaps: Sequence[CoverageGap] = ()
              ) -> Tuple[NormalizedSignal, ...]:
    """Classify every input, keeping its source and its source record."""
    index = _identity_index(alignments)
    signals: list = []

    for event in events:
        identity = index.get(event.activity_instance_ref, "")
        kind = _OUTCOME_SIGNALS.get(event.attempt_outcome)
        if kind is not None:
            signals.append(NormalizedSignal(
                kind=kind, source=SignalSource.CAREGIVER_OBSERVATION,
                source_ref=event.event_id, activity_identity_ref=identity))
        # Difficulty is a SECOND, independent fact about the same attempt.
        # JUST_RIGHT maps to nothing: it is not evidence for a change, and
        # inventing a signal for it would manufacture churn.
        difficulty_kind = (_DIFFICULTY_SIGNALS.get(event.difficulty)
                           if event.difficulty is not None else None)
        if difficulty_kind is not None:
            signals.append(NormalizedSignal(
                kind=difficulty_kind, source=SignalSource.CAREGIVER_OBSERVATION,
                source_ref=event.event_id, activity_identity_ref=identity))

    for signal in customization_signals:
        kind = _CUSTOMIZATION_SIGNALS.get(signal.signal_type)
        if kind is None:  # pragma: no cover - mapping is exhaustive
            continue
        signals.append(NormalizedSignal(
            kind=kind, source=SignalSource.PARENT_CUSTOMIZATION,
            source_ref=signal.signal_id,
            activity_identity_ref=index.get(signal.activity_instance_ref, "")))

    for record in defer_records:
        signals.append(NormalizedSignal(
            kind=SignalKind.PLAN_DEFERRED_BY_CAREGIVER,
            source=SignalSource.CAREGIVER_DEFER,
            source_ref=record.defer_id,
            activity_identity_ref=record.activity_identity_ref))

    for intervention in interventions:
        kind = _INTERVENTION_SIGNALS.get(intervention.action)
        if kind is None:  # pragma: no cover - mapping is exhaustive
            continue
        signals.append(NormalizedSignal(
            kind=kind, source=SignalSource.CLINICIAN_INTERVENTION,
            source_ref=intervention.intervention_id,
            activity_identity_ref=index.get(intervention.target_ref,
                                            intervention.target_ref or "")))

    for gap in coverage_gaps:
        signals.append(NormalizedSignal(
            kind=SignalKind.COVERAGE_GAP_RECORDED,
            source=SignalSource.PLANNER_CONDITION,
            source_ref=gap.gap_id,
            goal_ref_key=gap.goal_ref.as_key()))

    # Deterministic order so two runs over the same rows agree exactly.
    return tuple(sorted(signals, key=lambda s: s.as_key()))


def _event_directions(alignments: Sequence[ActivityGoalAlignment],
                      events: Sequence[ObservationEvent]
                      ) -> Dict[str, AdaptationDirection]:
    """Caregiver evidence sets NO difficulty direction. P0-B1.

    Every observed identity is MAINTAIN on the difficulty axis, whatever the
    outcome or difficulty said. The 2026.10 table that read one `too_hard`,
    `wasnt_ready_yet` or `didnt_want_to_try` as "easier", and `too_easy` as
    "harder/progressed", is retired by founder decision; see the module
    docstring. Theme and reviewed-variation decisions are made by
    `repeated_signal_policy`, never here.
    """
    index = _identity_index(alignments)
    directions: Dict[str, AdaptationDirection] = {}
    for event in events:
        identity = index.get(event.activity_instance_ref)
        if identity:
            directions.setdefault(identity, AdaptationDirection.MAINTAIN)
    return directions


def plan_adaptation(alignments: Sequence[ActivityGoalAlignment],
                    *,
                    events: Sequence[ObservationEvent] = (),
                    customization_signals: Sequence[ParentCustomizationSignal] = (),
                    defer_records: Sequence[DeferRecord] = (),
                    interventions: Sequence[TherapistIntervention] = (),
                    coverage_gaps: Sequence[CoverageGap] = (),
                    next_cycle_sequence: int) -> AdaptationPlan:
    """Decide what the next cycle should do. Pure and deterministic."""
    if next_cycle_sequence < 2:
        raise AdaptationEngineError(
            "adaptation targets cycle 2 onward; cycle 1 has no predecessor")

    # Superseded observations are never evidence. P0-B1.
    events = current_observations(tuple(events))
    signals = normalize(alignments, events, customization_signals,
                        defer_records, interventions, coverage_gaps)
    directions = _event_directions(alignments, events)
    index = _identity_index(alignments)

    # Family plan edits set NO autonomous difficulty direction either (P0-B1).
    # They remain normalized signals — provenance a clinician can read — but
    # the 2026.10 rule that turned MADE_EASIER / SWAPPED / REMOVED_FROM_WEEK
    # into "easier" and MADE_HARDER into "harder" is retired with the rest of
    # that table.
    for signal in customization_signals:
        identity = index.get(signal.activity_instance_ref)
        if identity:
            directions.setdefault(identity, AdaptationDirection.MAINTAIN)

    # Clinician direction is applied LAST and overrides, because it is a
    # clinical decision rather than an inference from evidence.
    future = [i for i in interventions
              if i.applies_to is InterventionScope.FUTURE_CYCLE]
    for intervention in future:
        identity = index.get(intervention.target_ref, intervention.target_ref)
        direction = _INTERVENTION_DIRECTIONS.get(intervention.action)
        if identity and direction is not None:
            directions[identity] = direction

    suppressed = set(
        record.activity_identity_ref for record in defer_records
        if record.suppresses(next_cycle_sequence) and not record.was_overridden)
    for identity, direction in list(directions.items()):
        if direction is AdaptationDirection.SUPPRESSED:
            suppressed.add(identity)
    for identity in suppressed:
        directions[identity] = AdaptationDirection.SUPPRESSED

    has_clinician = any(s.source is SignalSource.CLINICIAN_INTERVENTION
                        for s in signals)
    has_other = any(s.source is not SignalSource.CLINICIAN_INTERVENTION
                    for s in signals)
    if has_clinician and has_other:
        origin = AdaptationOrigin.MIXED
    elif has_clinician:
        origin = AdaptationOrigin.THERAPIST_DIRECTED
    else:
        origin = AdaptationOrigin.AUTOMATIC

    guidance = tuple(sorted(
        (i.target_ref, i.guidance_text) for i in future
        if i.action is InterventionAction.ADD_GUIDANCE and i.guidance_text))

    return AdaptationPlan(
        directions=dict(sorted(directions.items())),
        signals=signals,
        origin=origin,
        suppressed_identity_refs=tuple(sorted(suppressed)),
        carried_guidance=guidance,
        rule_version=ADAPTATION_RULE_VERSION,
    )


def describe_change(plan: AdaptationPlan) -> str:
    """A short, PHI-free summary of what changed and why.

    Counts of rule outcomes only — no activity name, no goal text, no
    rationale. It is a label for a record, not a clinical narrative.
    """
    tally: Dict[str, int] = {}
    for direction in plan.directions.values():
        tally[direction.value] = tally.get(direction.value, 0) + 1
    parts = [f"{name}={count}" for name, count in sorted(tally.items())]
    return f"origin={plan.origin.value}; " + ("; ".join(parts) or "no_change")
