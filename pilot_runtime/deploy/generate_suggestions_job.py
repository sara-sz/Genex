"""The OFFLINE suggestion-generation entrypoint — October pilot 0.5E-B.

This is the production execution path for `GoalService.generate_suggestions`,
and the only place a `SuggestionCanonicalAnchor` is ever written. It runs as a
batch job on the GENERATION image (`Dockerfile.generation`), never inside the
served API.

## STATUS — REAL SUGGESTION GENERATION IS NOT OPERATIONAL YET

Stated plainly, because the rest of this module describes machinery that does
work and it would be easy to read more into it than is true.

`main()` returns exit 3. The canonical-rung bridge, the generation image and
the path from a resolved rung to the frozen `generate_suggestions` boundary
are complete, tested and proven against the real Parent content. What does NOT
exist is the step before all of that: **nothing reads a child's stored Parent
baseline/observation and turns it into the observation tuples this module
consumes.**

That gap is **NOT Week 1 activity-generation work.** It is a remaining real
**Parent onboarding → suggestion-generation integration blocker**, and it is
pilot-critical. 0.5E-B is therefore the *SLP taxonomy coverage + canonical-rung
generation adapter foundation* — not a working generation pipeline.

The next pilot-critical slice wires:

    stored Parent baseline/observation
        -> this generation job
        -> CanonicalRung-enriched ObservationSnapshot
        -> deterministic GoalSuggestion

and lands BEFORE any goal -> Week 1 activity generation work begins.

## Why generation is a job and not a route

`generate_suggestions` has no HTTP route: `/pilot/children/{id}/goal-suggestions`
is registered GET-only, so a browser can read suggestions and never create
them. That is deliberate and 0.5E-A depends on it — the approval path's
"copy, never derive" rule is only meaningful because a clinician's browser
cannot author the provenance it later approves.

Keeping generation offline also keeps the served image narrow: the canonical
rung has to come from the real Parent workbook, and the workbook needs pandas,
openpyxl and the Parent modules. None of that belongs in a PHI-reviewed web
service, and none of it is there.

## What this job does, in order

1. Builds the real runtime (Firestore repositories, real token decoder).
2. Builds the canonical-rung bridge over the real Parent workbook + taxonomy.
3. For each observed domain, asks the bridge for the rung the OBSERVATION
   names — and attaches it only if the bridge returns one.
4. Hands the whole `ObservationSnapshot` to the existing, frozen
   `generate_suggestions` boundary, which persists suggestions and anchors.

Step 4 is the trusted boundary 0.5E-A already established. This job adds no
new write path: it does not touch `pilot_suggestion_anchors` itself, does not
create goals, and does not allocate anything.

## Fail closed, never substitute

If the bridge cannot produce a rung — the milestone is absent, an activity
family does not resolve in the taxonomy, or no functional-baseline track
declares the subdomain — the domain is passed through with NO
`canonical_rung`. It is never given a different rung, never given a guessed
family, and never given a rung from an adjacent month.

The consequence is intentional and visible: that domain's suggestion is
unanchored, so if a clinician approves it the resulting goal has no anchor,
and `allocate_goal` refuses to let it drive activities. The gap surfaces to a
human at the point of use instead of being silently filled.

Four of the 21 rungs on the declared Talking & Communicating baseline track are
in exactly this state after Scenario C — three blocked by genuinely missing
activity families and one by an ambiguous identifier, all explicitly deferred
to October post-pilot content review.

## Identity stays server-derived

The job takes a child id and a cycle month. It does not accept a domain, a
milestone, a months value, a family or a rung ref, so nothing a caller passes
can become provenance. The observation itself is read from stored records, and
the rung is looked up from the workbook — the two inputs the design trusts.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence, Tuple

from pilot_backend.goals.service import GoalService
from pilot_backend.goals.suggestion_engine import (
    EvidenceSource,
    ObservationSnapshot,
    ObservedDomain,
)
from pilot_backend.integration.gold_standard_source import (
    GoldStandardRungSource,
    RungTarget,
    try_rung_for_target,
)


def observed_domains_with_rungs(
    source: GoldStandardRungSource,
    observations: Sequence[Tuple[str, bool, EvidenceSource, str, str, bool,
                                 Optional[int], Optional[str]]],
) -> Tuple[Tuple[ObservedDomain, ...], Tuple[str, ...]]:
    """Attach a canonical rung to each observation that resolves to one.

    Returns the domains and a list of human-readable notes about every domain
    left unanchored, so the job's own output says which gaps a clinician will
    meet. Silence about a skipped domain would make a partial generation look
    complete.

    The last two elements of each observation are the TARGET: the rung months
    and milestone text the observation points at. They come from stored
    developmental input, not from a caller, and when either is absent the
    domain is simply unanchored.
    """
    domains: List[ObservedDomain] = []
    notes: List[str] = []
    for (key, answered, evidence, area, level, selected,
         target_months, target_milestone) in observations:
        rung = None
        if target_months is not None and target_milestone:
            rung = try_rung_for_target(source, RungTarget(
                domain_key=key,
                source_rung_months=target_months,
                milestone_text=target_milestone,
            ))
            if rung is None:
                # No milestone text in the note: it is clinical content, and
                # this line goes to a job log. The months and the domain are
                # enough for a reviewer to find the rung in the workbook.
                notes.append(
                    f"{key}: no canonical rung at {target_months}mo — the "
                    f"suggestion will be UNANCHORED and cannot drive "
                    f"activities until the gap is reviewed")
        else:
            notes.append(f"{key}: the observation names no target rung — "
                         f"the suggestion will be UNANCHORED")
        domains.append(ObservedDomain(
            domain_key=key, answered=answered, evidence_source=evidence,
            functional_baseline_area=area, observed_level=level,
            explicitly_selected=selected, canonical_rung=rung))
    return tuple(domains), tuple(notes)


def generate_for_child(
    *,
    goals: GoalService,
    principal,
    child_id: str,
    cycle_month: str,
    source: GoldStandardRungSource,
    observations: Sequence[Tuple[str, bool, EvidenceSource, str, str, bool,
                                 Optional[int], Optional[str]]],
    request_id: str = "",
):
    """One child-month through the frozen generation boundary.

    Refuses to run twice for the same child: `generate_suggestions` only ever
    creates, so calling it again would add a second parallel set of candidates
    for one month rather than replacing the first. The existing set is left
    exactly as it is, including any a clinician has already acted on.
    """
    already = goals.list_suggestions(principal, child_id)
    existing_for_month = [s for s in already if s.cycle_month == cycle_month]
    if existing_for_month:
        raise RuntimeError(
            f"{len(existing_for_month)} suggestions already exist for this "
            f"child-month; generation only creates, so re-running would "
            f"duplicate rather than replace them")

    domains, notes = observed_domains_with_rungs(source, observations)
    for note in notes:
        print(f"  UNANCHORED  {note}")

    snapshot = ObservationSnapshot(child_id=child_id, cycle_month=cycle_month,
                                   domains=domains)
    created = goals.generate_suggestions(principal, child_id, snapshot,
                                         request_id=request_id)
    anchored = sum(1 for d in domains if d.canonical_rung is not None)
    print(f"  generated   {len(created)} suggestions, "
          f"{anchored}/{len(domains)} observed domains anchored")
    return created


def main(argv: Sequence[str]) -> int:  # pragma: no cover - job entrypoint
    """Deliberately NOT wired to a default child or a default month.

    The job requires both explicitly. A generation entrypoint that defaulted
    to "the current month for every child" is one mis-scheduled run away from
    writing a parallel candidate set for an entire caseload.

    Intentionally not callable without a cycle month either: "now" on a
    container clock crossing a month boundary would write to a month nobody
    chose.
    """
    if len(argv) < 3:
        print("usage: generate_suggestions_job.py <child_id> <cycle_month>",
              file=sys.stderr)
        return 2
    if not os.environ.get("PILOT_ENVIRONMENT"):
        print("PILOT_ENVIRONMENT must be set explicitly", file=sys.stderr)
        return 2
    print(
        "BLOCKED: no stored Parent baseline/observation source is wired.\n"
        "\n"
        "This is a remaining Parent onboarding -> suggestion-generation\n"
        "INTEGRATION BLOCKER, not Week 1 activity-generation work. Real\n"
        "suggestion generation is NOT operational.\n"
        "\n"
        "In place and proven: the canonical-rung bridge over the real Parent\n"
        "workbook and taxonomy, the separate generation image, and the path\n"
        "from a resolved rung through the frozen generate_suggestions\n"
        "boundary. Missing: the read that turns a child's stored Parent\n"
        "baseline into observations for that path.\n", file=sys.stderr)
    return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
