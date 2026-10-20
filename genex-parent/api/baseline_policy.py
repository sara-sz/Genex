"""api/baseline_policy.py — versioned, immutable Parent baseline interview policy.

Slice 1 of the founder-approved 6/9 Baseline v2 redesign. See the canonical
design note: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## THE ONE PRINCIPLE THIS MODULE EXISTS TO ENFORCE

    Configuration controls the interview BUDGET.
    Evidence semantics remain INVARIANT.

A policy says how many questions the interview may spend, where to start
routing, which profile areas to try and in what order. It says NOTHING about
what an answer means. No answer-to-state mapping, no completeness rule, no
concern rule, no target-eligibility rule lives here, and
`POLICY_KEYS` is an exact allowlist so one cannot be added without a reviewer
noticing. Two tests hold the line:

  * the key set must equal `POLICY_KEYS` exactly;
  * the functions that decide meaning — `_classify`, `band_assessment`,
    `band_skill_rows` — must accept no policy argument.

So changing 6 to 7, or +6m to +9m, cannot make a skill demonstrated, cannot
turn `not_assessed` into evidence, and cannot make a `no` into a concern.

## POLICIES ARE IMMUTABLE; ONLY THE DEFAULT POINTER IS OPERATIONAL

A registered policy is frozen forever, because sessions reference it by id and
replay through it. Editing one in place would silently rewrite the meaning of
every historical interview that used it — which is exactly the failure the
digest exists to catch. The only operational change is which policy id new
sessions get (`_DEFAULTS`). Rollback is therefore repointing that, with no data
migration and no record rewrite.

Any value change — budgets, lookahead, priorities, required areas, the
target-finding budget — is a NEW policy id. There is no in-place tuning.

## FROZEN PER SESSION

`start()` resolves the default once and writes `policy_id` + `policy_digest`
onto the baseline record. Every later question, and every replay, resolves the
policy the RECORD names rather than today's default. A config change can
therefore never alter an in-progress or historical session.

The digest is stored as well as the id because an id alone would not detect a
registry edit. On replay both are checked: an unknown id or a digest mismatch
REFUSES rather than falling back to the current default, because a silent
fallback is how a historical session would quietly acquire today's rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

#: The canonical stored/compared shape of a policy. EXACT: a key outside this
#: tuple is a bug, and a reviewer adding one has to come through here. This is
#: the allowlist that keeps interview configuration and evidence semantics
#: apart — see the module docstring.
POLICY_KEYS: Tuple[str, ...] = (
    "policy_id",
    "baseline_version",
    "domain",
    # -- interview budget ------------------------------------------------
    "normal_question_target",
    "hard_question_max",
    # DERIVED, never a caller input. See `additional_question_allowance`.
    "additional_question_allowance",
    # -- routing ---------------------------------------------------------
    "normal_upper_lookahead_months",
    "anchor_source",
    "anchor_may_exceed_guardrail",
    "descent_lower_bound",
    # -- target finding (Channel A) --------------------------------------
    "target_finding_normal_allocation",
    "target_budget_enforced_at",
    "floor_rule",
    "floor_recovery_via_clarifier",
    "substitute_served_target",
    # -- profile (Channel B) ---------------------------------------------
    "required_profile_areas",
    "profile_area_priority",
    "profile_fallback_tiers",
    "profile_first_probe_anchor",
    "profile_clarify_direction",
)

#: Key fragments that would mean a policy had started to express MEANING rather
#: than budget. Asserted against `POLICY_KEYS` by a test, so this list is a
#: tripwire for a future edit rather than a runtime check.
FORBIDDEN_POLICY_KEY_FRAGMENTS: Tuple[str, ...] = (
    "demonstrated", "emerging", "not_assessed", "unknown", "concern",
    "mastered_if", "classify", "state_map", "answer_map", "eligib",
    "completeness_rule", "score", "weight", "threshold",
)


class BaselinePolicyError(Exception):
    """A policy could not be resolved or failed verification. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class BaselinePolicy:
    """One immutable interview policy, addressed by `policy_id`.

    Tuples rather than lists throughout, so a caller cannot mutate a registered
    policy through a shared reference.
    """

    policy_id: str
    baseline_version: str
    domain: str

    normal_question_target: int
    hard_question_max: int

    normal_upper_lookahead_months: int
    anchor_source: str
    anchor_may_exceed_guardrail: bool
    descent_lower_bound: str

    target_finding_normal_allocation: int
    target_budget_enforced_at: str
    floor_rule: str
    floor_recovery_via_clarifier: bool
    substitute_served_target: bool

    required_profile_areas: Tuple[str, ...]
    profile_area_priority: Tuple[str, ...]
    profile_fallback_tiers: Tuple[str, ...]
    profile_first_probe_anchor: str
    profile_clarify_direction: str

    # -- derived, never an input ------------------------------------------

    def __post_init__(self) -> None:
        """Validate the budget relationships at construction.

        A policy that cannot be satisfied is refused here rather than
        producing an interview nobody can finish.
        """
        if int(self.hard_question_max) < int(self.normal_question_target):
            raise BaselinePolicyError(
                "hard_question_max must be at least normal_question_target")
        if int(self.target_finding_normal_allocation) > int(
                self.normal_question_target):
            raise BaselinePolicyError(
                "the preferred Channel-A allocation cannot exceed the normal "
                "total interview target")
        if int(self.target_finding_normal_allocation) < 1:
            raise BaselinePolicyError(
                "the preferred Channel-A allocation must admit one question")

    @property
    def additional_question_allowance(self) -> int:
        """Questions 7-9: `hard_question_max - normal_question_target`.

        DERIVED, never accepted as policy input. It was briefly an independent
        field and that was a latent contradiction: a policy could declare
        `hard_question_max = 8` and an allowance of 3, and nothing would
        notice. One source of truth, computed.

        These are no longer "clarification" questions in particular. They are
        the additional-question allowance, and the founder's approved uses are
        target/floor completion, profile coverage recovery, uncertainty
        clarification and multi-area differentiation. Naming them after one of
        those four would have mis-described the other three.
        """
        return int(self.hard_question_max) - int(self.normal_question_target)

    # -- canonical form ---------------------------------------------------

    def as_canonical_dict(self) -> Dict[str, Any]:
        """The exact dict the digest is taken over.

        Built by naming every key from `POLICY_KEYS` rather than from
        `__dict__`, so a field added to the dataclass without being added to
        the allowlist does not silently enter the digest.
        """
        raw = {
            "policy_id": self.policy_id,
            "baseline_version": self.baseline_version,
            "domain": self.domain,
            "normal_question_target": int(self.normal_question_target),
            "hard_question_max": int(self.hard_question_max),
            "additional_question_allowance":
                int(self.additional_question_allowance),
            "normal_upper_lookahead_months":
                int(self.normal_upper_lookahead_months),
            "anchor_source": self.anchor_source,
            "anchor_may_exceed_guardrail":
                bool(self.anchor_may_exceed_guardrail),
            "descent_lower_bound": self.descent_lower_bound,
            "target_finding_normal_allocation":
                int(self.target_finding_normal_allocation),
            "target_budget_enforced_at": self.target_budget_enforced_at,
            "floor_rule": self.floor_rule,
            "floor_recovery_via_clarifier":
                bool(self.floor_recovery_via_clarifier),
            "substitute_served_target": bool(self.substitute_served_target),
            "required_profile_areas": list(self.required_profile_areas),
            "profile_area_priority": list(self.profile_area_priority),
            "profile_fallback_tiers": list(self.profile_fallback_tiers),
            "profile_first_probe_anchor": self.profile_first_probe_anchor,
            "profile_clarify_direction": self.profile_clarify_direction,
        }
        missing = set(POLICY_KEYS) - set(raw)
        extra = set(raw) - set(POLICY_KEYS)
        if missing or extra:
            raise BaselinePolicyError(
                "the canonical policy shape disagrees with POLICY_KEYS")
        return raw

    @property
    def policy_digest(self) -> str:
        return _digest(self.as_canonical_dict())

    # -- derived budget helpers -------------------------------------------
    #
    # Arithmetic only. Each returns a NUMBER OF QUESTIONS, never a judgement
    # about evidence.

    def profile_slots_for(self, target_questions: int) -> int:
        """Normal-phase profile questions, given what Channel A spent.

        `normal_question_target - target_questions`, floored at zero and NOT
        capped at the number of standard areas: breadth comes first and spare
        normal slots are filled with profile depth, which is the founder's
        "6 normally really means 6" correction.
        """
        return max(0, int(self.normal_question_target) - int(target_questions))

    def ascent_ceiling_months(self, chronological_months: int) -> int:
        """The upper ROUTING guardrail in months.

        Not a developmental age, not an expected score, not evidence of
        mastery, not a diagnosis and not a lower bound. It exists so a somewhat
        advanced child is noticed without routinely being asked substantially
        future skills.
        """
        return int(chronological_months) + int(
            self.normal_upper_lookahead_months)


def canonical_json(payload: Any) -> str:
    """One serialisation, so two equal policies digest equally."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

#: SUPERSEDED by policy 2. Retained, registered and immutable so that any
#: session which named it still replays through its own rules — never through
#: today's default. No deployed or real-family session was ever created under
#: it, which is why correcting the budget SEMANTICS was possible at all: the
#: correction landed as a new id rather than as an edit.
#:
#: Its values are identical to policy 2's. The difference is interpretation,
#: and that is exactly why it needed a new id: under policy 1 the Channel-A
#: allocation was read as an absolute ceiling, which blocked entering a whole
#: band that target integrity required.
TALKING_V2_POLICY_1 = BaselinePolicy(
    policy_id="talking-v2-policy-1",
    baseline_version="parent-2.4-functional-baseline-v2",
    domain="talking_and_communicating",

    normal_question_target=6,
    hard_question_max=9,

    normal_upper_lookahead_months=6,
    anchor_source="entry_choice",
    anchor_may_exceed_guardrail=True,
    descent_lower_bound="none",

    target_finding_normal_allocation=4,
    target_budget_enforced_at="complete_band_boundary",
    floor_rule="mastered_band_below",
    floor_recovery_via_clarifier=True,
    substitute_served_target=False,

    required_profile_areas=("receptive", "functional", "social"),
    profile_area_priority=("receptive", "functional", "social"),
    profile_fallback_tiers=("receptive_plus", "functional_plus",
                            "social_plus", "intelligibility"),
    profile_first_probe_anchor="chronological_with_guardrail",
    profile_clarify_direction="downward_unbounded",
)

#: THE CURRENT PILOT POLICY. Same values as policy 1; corrected semantics.
#:
#: `target_finding_normal_allocation = 4` is the PREFERRED Channel-A
#: allocation, chosen from the traversal audit — it holds Channel A to a mean
#: of 2.67 questions while leaving two normal slots for profile breadth. It is
#: NOT an absolute ceiling. When target or floor integrity requires another
#: COMPLETE band, Channel A may exceed it, admitted at the first bound the
#: whole band fits:
#:
#:     tier 1   within target_finding_normal_allocation (4)   preferred
#:     tier 2   within normal_question_target (6)             still a normal
#:                                                            interview, NOT an
#:                                                            additional question
#:     tier 3   within hard_question_max (9)                  consumes the
#:                                                            additional-question
#:                                                            allowance
#:     else     do not enter; stop honestly, never truncate
#:
#: Worked examples the tests pin: 24m mastered then band 30 costs 1+4=5, which
#: is tier 2 and leaves question 6 for Channel B with zero additional-question
#: usage. A 36m anchor needing the 30m floor costs 3+4=7, which is tier 3 and
#: makes question 7 the first beyond the normal interview.
#:
#: The bounds are enforced over the WHOLE interview, Channel A and Channel B
#: together. Nine is a total, never a per-channel figure.
TALKING_V2_POLICY_2 = BaselinePolicy(
    policy_id="talking-v2-policy-2",
    baseline_version="parent-2.4-functional-baseline-v2",
    domain="talking_and_communicating",

    normal_question_target=6,
    hard_question_max=9,

    normal_upper_lookahead_months=6,
    anchor_source="entry_choice",
    anchor_may_exceed_guardrail=True,
    descent_lower_bound="none",

    target_finding_normal_allocation=4,
    target_budget_enforced_at="complete_band_boundary",
    floor_rule="mastered_band_below",
    floor_recovery_via_clarifier=True,
    substitute_served_target=False,

    required_profile_areas=("receptive", "functional", "social"),
    profile_area_priority=("receptive", "functional", "social"),
    profile_fallback_tiers=("receptive_plus", "functional_plus",
                            "social_plus", "intelligibility"),
    profile_first_probe_anchor="chronological_with_guardrail",
    profile_clarify_direction="downward_unbounded",
)

_REGISTRY: Mapping[str, BaselinePolicy] = MappingProxyType({
    TALKING_V2_POLICY_1.policy_id: TALKING_V2_POLICY_1,
    TALKING_V2_POLICY_2.policy_id: TALKING_V2_POLICY_2,
})

#: `(baseline_version, domain) -> policy_id` for NEW sessions only. The one
#: operationally changeable thing in this module; rollback is repointing it.
_DEFAULTS: Mapping[Tuple[str, str], str] = MappingProxyType({
    (TALKING_V2_POLICY_2.baseline_version, TALKING_V2_POLICY_2.domain):
        TALKING_V2_POLICY_2.policy_id,
})


def registered_policy_ids() -> Tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def get_policy(policy_id: str) -> BaselinePolicy:
    """Resolve by id, or refuse. Never falls back to a default."""
    policy = _REGISTRY.get(str(policy_id or "").strip())
    if policy is None:
        raise BaselinePolicyError(
            "unknown baseline policy id; a session's policy must stay "
            "resolvable for replay to be deterministic")
    return policy


def default_policy(baseline_version: str, domain: str) -> BaselinePolicy:
    """The policy a NEW session for this (version, domain) is frozen to."""
    policy_id = _DEFAULTS.get((str(baseline_version), str(domain)))
    if policy_id is None:
        raise BaselinePolicyError(
            "no default baseline policy is registered for this domain")
    return get_policy(policy_id)


def verify_policy(policy_id: str, policy_digest: str) -> BaselinePolicy:
    """Resolve a session's frozen policy and prove it has not been edited.

    A mismatch means the registry changed under a session that already
    referenced it. Refusing is the only safe answer: continuing would apply
    today's rules to an interview conducted under different ones, and the
    record would no longer describe how it was produced.
    """
    policy = get_policy(policy_id)
    want = policy.policy_digest
    if str(policy_digest or "") != want:
        raise BaselinePolicyError(
            "the stored baseline policy digest does not match the registered "
            "policy; a registered policy is immutable and must never be edited")
    return policy


__all__ = [
    "FORBIDDEN_POLICY_KEY_FRAGMENTS",
    "POLICY_KEYS",
    "TALKING_V2_POLICY_1",
    "TALKING_V2_POLICY_2",
    "BaselinePolicy",
    "BaselinePolicyError",
    "canonical_json",
    "default_policy",
    "get_policy",
    "registered_policy_ids",
    "verify_policy",
]
