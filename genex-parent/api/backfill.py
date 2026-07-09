"""
api/backfill.py — Beta 2.3 Phase 1 (STAGING-ONLY) backfill of durable completion
history + all-time stars from pre-existing doc["feedback"] records.

Rules (honest, never fabricates):
  - Only feedback with completion=="did_it" becomes a completion + star.
  - Local date = created_at(UTC) + validated session timezone (NOT scheduled activity_date).
  - Missing/invalid tz → tz_source="default_utc", date_confidence="low", completeness="partial".
  - Unparseable created_at → "unavailable" (skipped; no star, no fabricated date).
  - Idempotent: skips any completion whose activity-day key already exists.
  - Low-confidence dates are still all-time stars but are excluded from streaks/milestone
    dates downstream (compute_week already drops date_confidence=="low").
Backfilled events carry actor_type="migration", provenance="migrated".
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from api import progress as pg


def backfill_doc(doc: Dict[str, Any], dry_run: bool = True) -> Tuple[Dict[str, int], bool]:
    """Return (stats, changed). When dry_run=True nothing is mutated.

    Effort stars: every eligible feedback (did_it / wasnt_ready_yet / didnt_want_to_try)
    becomes an ATTEMPT + star (once per activity/local date). Completions (milestone
    practice): only did_it. Never implies a non-did_it activity was completed."""
    session_id = doc.get("session_id", "")
    tz, tz_source = pg.validate_timezone(doc.get("timezone"))
    feedback = doc.get("feedback") or []
    attempt_index = dict(doc.get("attempt_index") or {})     # copies for dry-run safety
    comp_index = dict(doc.get("completion_index") or {})

    stats = {"feedback_examined": 0, "complete": 0, "partial": 0, "unavailable": 0,
             "stars_proposed": 0, "completions_proposed": 0, "skipped_existing": 0}
    new_attempts, new_completions, new_events = [], [], []
    date_confidence = "high" if tz_source == "session" else "low"

    for f in feedback:
        if f.get("completion") not in pg.ELIGIBLE_STAR_STATUSES:
            continue
        stats["feedback_examined"] += 1
        activity_id = f.get("activity_id") or ""
        utc = pg.parse_iso_utc(f.get("created_at") or f.get("completed_at_utc") or "")
        if utc is None or not activity_id:
            stats["unavailable"] += 1
            continue
        local_date = pg.local_date_for(utc, tz)
        att_key = pg.attempt_idem_key(session_id, activity_id, local_date)
        comp_key = pg.completion_idem_key(session_id, activity_id, local_date)

        has_title = bool(f.get("title"))
        completeness = "complete" if (f.get("metadata_found") and has_title) else "partial"

        snapshot = {
            "title": f.get("title", ""), "instructions": f.get("instructions", ""),
            "domain": f.get("domain", ""), "domain_label": f.get("domain_label", ""),
            "focus_key": f.get("focus_key", ""), "focus_label": f.get("focus_label", ""),
            "difficulty_level": f.get("difficulty_level", ""), "age_band": f.get("milestone_age_months"),
            "materials": "", "duration": "", "source_bank_type": f.get("source_bank_type", ""),
        }

        # Effort star (any eligible status).
        if att_key not in attempt_index:
            stats["complete" if completeness == "complete" else "partial"] += 1
            stats["stars_proposed"] += 1
            if dry_run:
                attempt_index[att_key] = "dry"
            else:
                star_event_id = pg.new_id("evt")
                attempt = pg.build_attempt_record(
                    session_id=session_id, owner_uid=doc.get("owner_uid", ""),
                    feedback_id=f.get("feedback_id", ""), idempotency_key=att_key,
                    status=f.get("completion"), completed_at_utc=utc.isoformat(), completion_tz=tz,
                    tz_source=tz_source, local_date=local_date, date_confidence=date_confidence,
                    plan_id=f.get("plan_id"), scheduled_day=f.get("day"), activity_instance_id=activity_id,
                    source=f.get("source", "primary"), module_id=f.get("module_id"),
                    provenance="original", snapshot=snapshot, star_event_id=star_event_id,
                )
                attempt["backfilled"] = True
                new_attempts.append(attempt)
                attempt_index[att_key] = attempt["attempt_id"]
                new_events.append(pg.build_event(
                    event_id=star_event_id, session_id=session_id, owner_uid=doc.get("owner_uid", ""),
                    event_type="star_awarded", source_record_id=attempt["attempt_id"],
                    created_at_utc=utc.isoformat(), local_event_date=local_date, idempotency_key=att_key,
                    actor_type="migration", provenance="migrated", rule_version=pg.STAR_RULE_VERSION,
                    metadata={"backfill": True, "awarded_for": "attempt", "status": f.get("completion")},
                ))
        else:
            stats["skipped_existing"] += 1

        # Completion (did_it only).
        if f.get("completion") == "did_it" and comp_key not in comp_index:
            stats["completions_proposed"] += 1
            if dry_run:
                comp_index[comp_key] = "dry"
            else:
                completion = pg.build_completion_record(
                    session_id=session_id, owner_uid=doc.get("owner_uid", ""),
                    feedback_id=f.get("feedback_id", ""), idempotency_key=comp_key,
                    completed_at_utc=utc.isoformat(), completion_tz=tz, tz_source=tz_source,
                    local_completion_date=local_date, scheduled_date=f.get("activity_date"),
                    date_confidence=date_confidence, data_completeness=completeness,
                    plan_id=f.get("plan_id"), plan_period_id=f.get("plan_period_id", ""),
                    cycle_week=int(f.get("cycle_week", 1) or 1), scheduled_day=f.get("day"),
                    activity_instance_id=activity_id, activity_template_id=None,
                    source=f.get("source", "primary"), module_id=f.get("module_id"),
                    provenance="original", source_activity_id=f.get("original_activity_id"),
                    generated_or_manual="generated", snapshot=snapshot,
                    milestone=pg.derive_milestone(f, f.get("domain", "")),
                    attempt_id=attempt_index.get(att_key),
                )
                completion["backfilled"] = True
                new_completions.append(completion)
                comp_index[comp_key] = completion["completion_id"]
                new_events.append(pg.build_event(
                    session_id=session_id, owner_uid=doc.get("owner_uid", ""), event_type="activity_completed",
                    source_record_id=completion["completion_id"], created_at_utc=utc.isoformat(),
                    local_event_date=local_date, idempotency_key=comp_key, actor_type="migration",
                    provenance="migrated", metadata={"backfill": True}))

    changed = bool(new_attempts or new_completions)
    if not dry_run and changed:
        doc.setdefault("attempts", []).extend(new_attempts)
        doc.setdefault("completions", []).extend(new_completions)
        doc.setdefault("events", []).extend(new_events)
        ai = doc.setdefault("attempt_index", {})
        for a in new_attempts:
            ai[a["idempotency_key"]] = a["attempt_id"]
        ci = doc.setdefault("completion_index", {})
        for c in new_completions:
            ci[c["idempotency_key"]] = c["completion_id"]
    return stats, changed
