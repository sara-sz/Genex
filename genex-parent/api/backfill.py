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
    """Return (stats, changed). When dry_run=True nothing is mutated."""
    session_id = doc.get("session_id", "")
    tz, tz_source = pg.validate_timezone(doc.get("timezone"))
    feedback = doc.get("feedback") or []
    comp_index = dict(doc.get("completion_index") or {})   # copy for dry-run safety

    stats = {"feedback_examined": 0, "complete": 0, "partial": 0,
             "unavailable": 0, "stars_proposed": 0, "skipped_existing": 0}
    new_completions = []
    new_events = []

    for f in feedback:
        if f.get("completion") != "did_it":
            continue
        stats["feedback_examined"] += 1
        activity_id = f.get("activity_id") or ""
        utc = pg.parse_iso_utc(f.get("created_at") or f.get("completed_at_utc") or "")
        if utc is None or not activity_id:
            stats["unavailable"] += 1
            continue
        local_date = pg.local_date_for(utc, tz)
        date_confidence = "high" if tz_source == "session" else "low"
        comp_key = pg.completion_idem_key(session_id, activity_id, local_date)
        if comp_key in comp_index:
            stats["skipped_existing"] += 1
            continue

        has_snapshot_title = bool(f.get("title"))            # legacy feedback rarely has a title
        completeness = "complete" if (f.get("metadata_found") and has_snapshot_title) else "partial"
        stats["complete" if completeness == "complete" else "partial"] += 1
        stats["stars_proposed"] += 1

        if dry_run:
            comp_index[comp_key] = "dry"   # avoid double-counting same key within this run
            continue

        milestone = pg.derive_milestone(f, f.get("domain", ""))
        snapshot = {
            "title": f.get("title", ""), "instructions": f.get("instructions", ""),
            "domain": f.get("domain", ""), "domain_label": f.get("domain_label", ""),
            "focus_key": f.get("focus_key", ""), "focus_label": f.get("focus_label", ""),
            "difficulty_level": f.get("difficulty_level", ""), "age_band": f.get("milestone_age_months"),
            "materials": "", "duration": "", "source_bank_type": f.get("source_bank_type", ""),
        }
        star_event_id = pg.new_id("evt")
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
            generated_or_manual="generated", snapshot=snapshot, milestone=milestone,
            star_event_id=star_event_id,
        )
        completion["backfilled"] = True
        new_completions.append(completion)
        comp_index[comp_key] = completion["completion_id"]
        for etype, actor, prov, rv in (
            ("activity_completed", "migration", "migrated", None),
            ("star_awarded", "migration", "migrated", pg.STAR_RULE_VERSION),
        ):
            new_events.append(pg.build_event(
                event_id=(star_event_id if etype == "star_awarded" else None),
                session_id=session_id, owner_uid=doc.get("owner_uid", ""), event_type=etype,
                source_record_id=completion["completion_id"], created_at_utc=utc.isoformat(),
                local_event_date=local_date, idempotency_key=comp_key, actor_type=actor,
                provenance=prov, rule_version=rv, metadata={"backfill": True},
            ))

    changed = bool(new_completions)
    if not dry_run and changed:
        doc.setdefault("completions", []).extend(new_completions)
        doc.setdefault("events", []).extend(new_events)
        doc.setdefault("completion_index", {})
        for c in new_completions:
            doc["completion_index"][c["idempotency_key"]] = c["completion_id"]
    return stats, changed
