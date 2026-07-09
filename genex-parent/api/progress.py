"""
api/progress.py — Beta 2.3 Phase 1: durable completion history, event ledger,
idempotency keys, timezone-safe local dates, and stars/week Progress computation.

Pure/deterministic helpers only — NO storage, auth, or HTTP here. The write path
(api/main.py /feedback) and the read path (GET …/progress) call these. genex_core
is never touched. Cups/badges/milestone-practice/check-ins are Phase 2–4; the
records below already carry the structured facts those phases need.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# ── Versions ────────────────────────────────────────────────────────────────
COMPLETION_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
PROGRESS_SCHEMA_VERSION = 1
MILESTONE_ID_VERSION = "mv1"
STAR_RULE_VERSION = "sv1"

_WEEK_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Star / attempt semantics (product correction 2026-07-09):
#   Any real parent attempt earns ONE effort star per activity instance per local
#   date. ONLY did_it creates a completion record and can contribute to future
#   milestone practice. Attempt records carry the exact outcome so the data model
#   never implies a non-did_it activity was "completed".
ELIGIBLE_STAR_STATUSES = ("did_it", "wasnt_ready_yet", "didnt_want_to_try")
OUTCOME_BY_STATUS = {
    "did_it": "completed",
    "wasnt_ready_yet": "not_ready",
    "didnt_want_to_try": "did_not_want",
}

# Phase 1 emits only these two; "daily_plan_completed" is Phase 2 (needs a stored
# daily-goal snapshot) and is intentionally NEVER returned in Phase 1.
STATUS_NO_PRACTICE = "no_practice"
STATUS_PRACTICED = "practiced"
STATUS_DAILY_PLAN_COMPLETED = "daily_plan_completed"  # reserved, not emitted in Phase 1


# ── Timezone-safe local dates (§7) ────────────────────────────────────────────

def validate_timezone(tz_str: Optional[str]) -> Tuple[str, str]:
    """Return (iana_tz, tz_source). tz_source ∈ {"session","default_utc"}.
    Never raises; an invalid/missing IANA name falls back to UTC (low confidence)."""
    tz = (tz_str or "").strip()
    if tz and ZoneInfo is not None:
        try:
            ZoneInfo(tz)
            return tz, "session"
        except Exception:
            return "UTC", "default_utc"
    if tz == "UTC":
        return "UTC", "session"
    return "UTC", "default_utc"


def _zone(tz_str: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_str)
        except Exception:
            return timezone.utc
    return timezone.utc


def local_date_for(utc_dt: datetime, tz_str: str) -> str:
    """Local calendar date (YYYY-MM-DD) of a UTC instant in tz_str."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(_zone(tz_str)).date().isoformat()


def week_bounds(local_iso: str) -> Tuple[str, str]:
    """Monday (start) and Sunday (end) ISO dates for the local week containing local_iso."""
    d = date.fromisoformat(local_iso)
    monday = d - timedelta(days=d.weekday())        # Monday=0
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def parse_iso_utc(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None."""
    if not s:
        return None
    try:
        t = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ── Idempotency keys (§6, clarification 3) ────────────────────────────────────

def _sha256(*parts: Any) -> str:
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()


def _norm_note(note: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (note or "").strip().lower())


def attempt_idem_key(session_id: str, activity_instance_id: str, local_date: str) -> str:
    """At most one effort star (attempt) per activity instance per local date —
    regardless of the outcome (did_it / not_ready / did_not_want)."""
    return _sha256("att1", session_id, activity_instance_id, local_date)


def completion_idem_key(session_id: str, activity_instance_id: str, local_completion_date: str) -> str:
    """At most one COMPLETION (did_it, milestone-practice unit) per activity
    instance per local date. Distinct namespace from the attempt/star key."""
    return _sha256("cmp1", session_id, activity_instance_id, local_completion_date)


def feedback_req_key(
    session_id: str, activity_instance_id: str, local_submission_date: str,
    completion: str, enjoyment: str, difficulty: str,
    discuss_with_care_team: bool, care_team_member: Optional[str],
    care_team_tags: Optional[List[str]], note: Optional[str],
) -> str:
    """Fingerprint of an exact feedback submission — dedupes identical retries,
    including non-did_it feedback. A changed value → different key → a revision."""
    tags = ",".join(sorted(care_team_tags or []))
    return _sha256(
        "fb1", session_id, activity_instance_id, local_submission_date,
        completion, enjoyment, difficulty, bool(discuss_with_care_team),
        care_team_member or "", tags, _norm_note(note),
    )


# ── Milestone identity (§8, clarification 3 — activity_family EXCLUDED) ────────

def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def derive_milestone(internal: Optional[Dict[str, Any]], domain: str) -> Dict[str, Any]:
    """Canonical milestone identity from enriched internal metadata. The ID identifies
    the MILESTONE, never the activity — activity_family/bridge_step are metadata only.
    Returns nulls (never fabricated) when identity cannot be resolved.

    Phase 1 stores this immutably on the completion; observable_text (parent_explanation)
    and final cup eligibility are resolved in Phase 4 (cups)."""
    internal = internal or {}
    dom = (internal.get("domain") or domain or "").strip()
    ms_text = (internal.get("milestone_text") or "").strip()
    ms_age = internal.get("milestone_age_months")
    bridge_text = (internal.get("bridge_step_text") or "").strip()
    bridge_num = internal.get("bridge_step_index")

    milestone_id: Optional[str] = None
    source: Optional[str] = None
    canonical_text = ""
    if dom and ms_text:
        source = "cdc"
        canonical_text = ms_text
        age = ms_age if ms_age is not None else "na"
        milestone_id = f"{MILESTONE_ID_VERSION}:cdc:{dom}:{age}:{_sha256(_norm_text(ms_text))[:12]}"
    elif dom and bridge_text:
        source = "bridge"
        canonical_text = bridge_text
        age = ms_age if ms_age is not None else "na"
        milestone_id = f"{MILESTONE_ID_VERSION}:bridge:{dom}:{age}:{_sha256(_norm_text(bridge_text))[:12]}"

    return {
        "milestone_id": milestone_id,
        "milestone_source": source,
        "short_label": canonical_text[:120] if canonical_text else "",
        "observable_text": None,               # resolved in Phase 4 (cups)
        "canonical_age_months": ms_age,
        "bridge_step_number": bridge_num,
        "supporting_milestone_ids": [],
        # provisional cup-track hint; Phase 4 additionally requires observable_text
        "cup_eligible": bool(milestone_id and source == "cdc"),
    }


# ── Record builders (§1, §2) ──────────────────────────────────────────────────

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


def build_event(
    *, session_id: str, owner_uid: str, event_type: str, source_record_id: str,
    created_at_utc: str, local_event_date: str, idempotency_key: Optional[str],
    actor_type: str, provenance: str, rule_version: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None, event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One append-only ledger event with explicit provenance (clarification 4)."""
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": event_id or new_id("evt"),
        "session_id": session_id,
        "owner_uid": owner_uid,
        "type": event_type,
        "source_record_id": source_record_id,
        "created_at_utc": created_at_utc,
        "local_event_date": local_event_date,
        "idempotency_key": idempotency_key,
        "actor_type": actor_type,               # parent | system | migration
        "provenance": provenance,               # parent_reported | app_recorded | system_calculated | migrated
        "rule_version": rule_version,
        "metadata": metadata or {},
    }


def build_attempt_record(
    *, session_id: str, owner_uid: str, feedback_id: str, idempotency_key: str,
    status: str, completed_at_utc: str, completion_tz: str, tz_source: str,
    local_date: str, date_confidence: str, plan_id: Optional[str],
    scheduled_day: Optional[str], activity_instance_id: str, source: str,
    module_id: Optional[str], provenance: str, snapshot: Dict[str, Any],
    star_event_id: str,
) -> Dict[str, Any]:
    """One immutable ATTEMPT record — a real parent engagement with an activity on a
    local date. Earns exactly one effort star. `outcome` distinguishes completed /
    not_ready / did_not_want; is_completion is True only for did_it. This never
    implies completion for non-did_it feedback."""
    week_start, week_end = week_bounds(local_date)
    return {
        "schema_version": 1,
        "attempt_id": new_id("att"),
        "idempotency_key": idempotency_key,
        "session_id": session_id,
        "owner_uid": owner_uid,
        "feedback_id": feedback_id,
        "activity_instance_id": activity_instance_id,
        "status": status,                                   # did_it | wasnt_ready_yet | didnt_want_to_try
        "outcome": OUTCOME_BY_STATUS.get(status, status),   # completed | not_ready | did_not_want
        "is_completion": status == "did_it",
        "completed_at_utc": completed_at_utc,
        "completion_tz": completion_tz,
        "tz_source": tz_source,
        "local_date": local_date,
        "date_confidence": date_confidence,
        "week_start": week_start,
        "week_end": week_end,
        "plan_id": plan_id,
        "scheduled_day": scheduled_day,
        "source": source,
        "module_id": module_id,
        "provenance": provenance,
        "snapshot": snapshot,
        "star_awarded": True,
        "star_event_id": star_event_id,
    }


def build_completion_record(
    *, session_id: str, owner_uid: str, feedback_id: str, idempotency_key: str,
    completed_at_utc: str, completion_tz: str, tz_source: str,
    local_completion_date: str, scheduled_date: Optional[str],
    date_confidence: str, data_completeness: str,
    plan_id: Optional[str], plan_period_id: Optional[str], cycle_week: int,
    scheduled_day: Optional[str], activity_instance_id: str,
    activity_template_id: Optional[str], source: str, module_id: Optional[str],
    provenance: str, source_activity_id: Optional[str], generated_or_manual: str,
    snapshot: Dict[str, Any], milestone: Dict[str, Any], attempt_id: Optional[str],
) -> Dict[str, Any]:
    """Immutable completion record for did_it ONLY (the milestone-practice unit, §1,
    §4). Links the attempt that earned the star (attempt_id); the effort star itself
    lives on the attempt, not here. No forward-mutable refs."""
    week_start, week_end = week_bounds(local_completion_date)
    return {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "completion_id": new_id("cmp"),
        "idempotency_key": idempotency_key,
        "session_id": session_id,
        "owner_uid": owner_uid,
        "feedback_id": feedback_id,
        "completed_at_utc": completed_at_utc,
        "completion_tz": completion_tz,
        "tz_source": tz_source,
        "local_completion_date": local_completion_date,
        "scheduled_date": scheduled_date,
        "week_start": week_start,
        "week_end": week_end,
        "date_confidence": date_confidence,
        "data_completeness": data_completeness,
        "plan_id": plan_id,
        "plan_period_id": plan_period_id,
        "cycle_week": cycle_week,
        "scheduled_day": scheduled_day,
        "activity_instance_id": activity_instance_id,
        "activity_template_id": activity_template_id,
        "source": source,
        "module_id": module_id,
        "provenance": provenance,
        "source_activity_id": source_activity_id,
        "generated_or_manual": generated_or_manual,
        "snapshot": snapshot,
        "milestone": milestone,
        "valid_completion": True,
        "completion_action": "feedback_did_it",
        "attempt_id": attempt_id,
    }


# ── Phase 2: categories & milestones we're working on ────────────────────────

PRACTICE_TARGET = 5

# Clean, short, parent-facing category labels for the Progress screen. Keyed by the
# stable Genex domain_key. Falls back to the source focus label if a key is unmapped.
PROGRESS_DOMAIN_LABELS = {
    "language_and_communication": "Speech & Communication",
    "social_and_emotional": "Social & Emotional",
    "cognitive": "Learning & Thinking",
    "movement_and_physical": "Movement & Daily Skills",
}

_OBSERVABLE_LOOKUP: Optional[Dict[Tuple[str, str], str]] = None


def _observable_lookup() -> Dict[Tuple[str, str], str]:
    """Cached {(category_key, norm(milestone_text)): parent_explanation} from the CDC
    table (read-only use of genex_core — never modifies it). Empty on any failure so
    observable_text degrades to null rather than raising."""
    global _OBSERVABLE_LOOKUP
    if _OBSERVABLE_LOOKUP is None:
        lookup: Dict[Tuple[str, str], str] = {}
        try:
            from genex_core.milestones import get_cdc_df  # read-only
            df = get_cdc_df()
            for _, r in df.iterrows():
                ck = str(r.get("category_key", "") or "").strip()
                ms = str(r.get("milestone", "") or "").strip()
                pe = str(r.get("parent_explanation", "") or "").strip()
                if ck and ms and pe:
                    lookup[(ck, _norm_text(ms))] = pe
        except Exception:
            lookup = {}
        _OBSERVABLE_LOOKUP = lookup
    return _OBSERVABLE_LOOKUP


def resolve_observable_text(domain: str, short_label: str) -> Optional[str]:
    """Parent-friendly observable sentence for a milestone, or None if not resolvable.
    Never fabricated."""
    if not domain or not short_label:
        return None
    return _observable_lookup().get((domain, _norm_text(short_label)))


def _milestone_reliable(milestone: Dict[str, Any], domain: str) -> bool:
    """A milestone row may appear in categories_in_practice only when its mapping is
    reliable: a canonical CDC milestone_id + a known domain."""
    return bool((milestone or {}).get("milestone_id")) and (milestone or {}).get("milestone_source") == "cdc" and bool(domain)


def compute_categories_in_practice(
    completions: List[Dict[str, Any]],
    active_milestones: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """`Categories & Milestones We're Working On`, grouped by developmental domain.

    Milestone-practice dots come ONLY from did_it COMPLETIONS with a reliable canonical
    CDC milestone_id + domain (non-did_it never creates a completion, so it can never
    fill a dot). Different activities for the same canonical milestone aggregate into
    ONE row (the id excludes activity_family). Duplicate did_it is already deduped by
    the completion index, so it can't double-count. `active_milestones` (reliable
    milestones from the current plan) seed rows at 0 practices so parents can see what
    they're working on. Unreliable/unmapped completions never create a row (they still
    earned stars via attempts). No cups, no check-ins. practices capped at 5 for the UI;
    the true count is preserved for analytics."""
    from api.focus_selector import FOCUS_LABELS  # local import avoids import cycle

    per: Dict[str, Dict[str, Any]] = {}

    def _seed(mid, source, short_label, domain, age, cup):
        return per.setdefault(mid, {
            "milestone_id": mid, "milestone_source": source, "short_label": short_label or "",
            "domain": domain, "canonical_age_months": age, "cup_eligible": bool(cup),
            "count": 0, "hi_dates": set(),
        })

    for c in completions or []:
        if not c.get("valid_completion", True):
            continue
        ms = c.get("milestone") or {}
        domain = (c.get("snapshot") or {}).get("domain") or ""
        if not _milestone_reliable(ms, domain):
            continue
        e = _seed(ms["milestone_id"], "cdc", ms.get("short_label"), domain,
                  ms.get("canonical_age_months"), ms.get("cup_eligible"))
        e["count"] += 1                                   # completions are already unique per activity/date
        d = c.get("local_completion_date")
        if d and c.get("date_confidence") != "low":       # distinct DAYS from reliable dates only
            e["hi_dates"].add(d)

    for am in active_milestones or []:
        if _milestone_reliable(am, am.get("domain")) and am["milestone_id"] not in per:
            _seed(am["milestone_id"], "cdc", am.get("short_label"), am.get("domain"),
                  am.get("canonical_age_months"), am.get("cup_eligible"))

    domains: Dict[str, Dict[str, Any]] = {}
    for e in per.values():
        true_count = e["count"]
        row = {
            "milestone_id": e["milestone_id"],
            "milestone_source": e["milestone_source"],
            "short_label": e["short_label"],
            "observable_text": resolve_observable_text(e["domain"], e["short_label"]),
            "practices_completed": min(true_count, PRACTICE_TARGET),   # capped for the UI
            "practices_completed_true": true_count,                    # preserved for analytics
            "practices_target": PRACTICE_TARGET,
            "distinct_practice_days": len(e["hi_dates"]),
            "check_in_ready": False,                                   # Phase 4 — never true in Phase 2
            "practice_ready": true_count >= PRACTICE_TARGET,          # informational (no check-in created)
            "cup_eligible": bool(e["cup_eligible"]),
        }
        source_label = FOCUS_LABELS.get(e["domain"], e["domain"])
        g = domains.setdefault(e["domain"], {
            "domain_key": e["domain"],
            "domain_label": PROGRESS_DOMAIN_LABELS.get(e["domain"], source_label),  # clean, parent-facing
            "source_domain_label": source_label,                                    # original, for reporting
            "milestones": [],
        })
        g["milestones"].append(row)

    out: List[Dict[str, Any]] = []
    for dk in sorted(domains):
        g = domains[dk]
        g["milestones"].sort(key=lambda m: (-m["practices_completed_true"], m["short_label"]))
        out.append(g)
    return out


# ── Progress read computation (§3) ────────────────────────────────────────────

def compute_stars(attempts: List[Dict[str, Any]], week_start: str, week_end: str) -> Dict[str, int]:
    """Stars = effort ATTEMPTS (any eligible feedback), deduped by idempotency_key —
    one per activity instance per local date. this_week bounded to the current local
    Mon–Sun; all_time = all. Never negative. (Milestone practice uses completions, not
    this.)"""
    seen = set()
    all_time = 0
    this_week = 0
    for a in attempts or []:
        k = a.get("idempotency_key") or a.get("attempt_id")
        if k in seen:
            continue
        seen.add(k)
        all_time += 1
        d = a.get("local_date") or a.get("local_completion_date") or ""
        if week_start <= d <= week_end:
            this_week += 1
    return {"this_week": this_week, "all_time": all_time}


def compute_week(attempts: List[Dict[str, Any]], today_local_iso: str) -> List[Dict[str, Any]]:
    """Exactly 7 ordered Monday→Sunday entries for the local week of today. A day is
    `practiced` when the parent engaged with ≥1 activity (any eligible attempt) that
    day — effort, matching stars. Phase 1 status ∈ {no_practice, practiced};
    daily_plan_completed (all planned did_it) is Phase 2 and never emitted here."""
    week_start, _ = week_bounds(today_local_iso)
    monday = date.fromisoformat(week_start)
    practiced_dates = {
        a.get("local_date")
        for a in (attempts or [])
        if a.get("date_confidence") != "low"
    }
    week: List[Dict[str, Any]] = []
    for i in range(7):
        d = (monday + timedelta(days=i)).isoformat()
        week.append({
            "date": d,
            "day": _WEEK_DAYS[i],
            "status": STATUS_PRACTICED if d in practiced_dates else STATUS_NO_PRACTICE,
            "is_today": d == today_local_iso,
        })
    return week


def build_progress_response(
    *, session_id: str, timezone_str: str, attempts: List[Dict[str, Any]],
    completions: Optional[List[Dict[str, Any]]] = None,
    active_plan_milestones: Optional[List[Dict[str, Any]]] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Full versioned Progress payload. Stars + weekly circles are effort-based
    (attempts). `categories_in_practice` (Phase 2) is milestone practice grouped by
    domain, sourced ONLY from did_it completions (+ reliable current-plan milestones at
    0). No cups/check-ins. Reserved future arrays present but empty."""
    tz, _src = validate_timezone(timezone_str)
    now = now_utc or datetime.now(timezone.utc)
    today_local = local_date_for(now, tz)
    week_start, week_end = week_bounds(today_local)
    return {
        "progress_schema_version": PROGRESS_SCHEMA_VERSION,
        "session_id": session_id,
        "timezone": tz,
        "week": compute_week(attempts, today_local),
        "stars": compute_stars(attempts, week_start, week_end),
        "categories_in_practice": compute_categories_in_practice(
            completions or [], active_plan_milestones or []),
        "latest_wins": [],               # Phase 2+ (badges/cups only — never plain stars)
        "badges": [],                    # Phase 2 (badges)
        "milestones_in_practice": [],    # backward-compat placeholder (grouped view is categories_in_practice)
        "checkins_ready": [],            # Phase 4
        "cups_by_domain": [],            # Phase 4
    }
