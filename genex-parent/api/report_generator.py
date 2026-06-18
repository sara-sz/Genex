"""
api/report_generator.py — Phase 1 care team report generation.

Generates plain-text summaries for four report types. All reports are
template-based (no LLM calls) in Phase 1.

Privacy rules:
  - "your child" is used throughout.
  - The child's real name is never in the session document, so it cannot
    appear in reports regardless of this module's code.
  - brain_state, plan_internal, gate_report, and debug fields are never
    included in the report body.

Do NOT import from app.py or Streamlit. Do NOT import from genex_core.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api.adapters import DOMAIN_LABELS

# ── Report metadata ─────────────────────────────────────────────────────────

REPORT_TITLES: Dict[str, str] = {
    "doctor":                   "Doctor Report",
    "speech_therapist":         "Speech Therapist Report",
    "occupational_therapist":   "Occupational Therapist Report",
    "physical_therapist":       "Physical Therapist Report",
    # Beta 2.0 combined OT/PT report.
    "ot_pt":                    "Occupational / Physical Therapy Report",
}

_REPORT_OPENERS: Dict[str, str] = {
    "doctor": (
        "This summary was prepared for your child's doctor or paediatrician. "
        "It covers current developmental focus areas and home practice activity data "
        "from the Genex programme."
    ),
    "speech_therapist": (
        "This summary was prepared for your child's speech-language therapist. "
        "It covers language and communication practice activities and parent-reported progress."
    ),
    "occupational_therapist": (
        "This summary was prepared for your child's occupational therapist. "
        "It covers fine motor, daily-living, and adaptive skill practice activities "
        "and parent-reported progress."
    ),
    "physical_therapist": (
        "This summary was prepared for your child's physical therapist. "
        "It covers movement and physical development practice activities "
        "and parent-reported progress."
    ),
    "ot_pt": (
        "This summary was prepared for your child's occupational and/or physical "
        "therapist. It covers movement, motor, sensory/regulation, and adaptive "
        "self-help practice activities and parent-reported progress."
    ),
}


# ── Care-team note routing (Beta 2.0, Step 3B) ──────────────────────────────
# Single source of truth for report visibility. Mirrored in
# docs/care_team_report_routing.md and exercised by tests/test_care_team_routing.py.

# Normalized provider tags a parent can attach to a note.
CARE_TEAM_TAGS = ("doctor", "st", "ot_pt")

# Legacy single-select care_team_member → normalized tag.
LEGACY_MEMBER_TO_TAG: Dict[str, str] = {
    "Doctor": "doctor",
    "ST": "st",
    "OT": "ot_pt",
    "PT": "ot_pt",
}

# Report type → the provider bucket whose notes it shows.
# Legacy occupational_therapist / physical_therapist both fold into ot_pt for now.
REPORT_TYPE_TO_PROVIDER: Dict[str, str] = {
    "doctor": "doctor",
    "speech_therapist": "st",
    "occupational_therapist": "ot_pt",
    "physical_therapist": "ot_pt",
    "ot_pt": "ot_pt",
}


def compute_note_visibility(record: Dict[str, Any]) -> List[str]:
    """Return the normalized provider tags a feedback note is visible to.

    Resolution order (backward-compatible):
      1. explicit care_team_tags (Beta 2.0) — kept if valid;
      2. else legacy care_team_member mapped via LEGACY_MEMBER_TO_TAG;
      3. else (flagged note with neither) → ["doctor"] only.

    This is the *parent note visibility* set. It is independent of activity
    relevance. The doctor report shows every flagged note regardless of this set
    (see note_visible_in_report).
    """
    tags = record.get("care_team_tags")
    if tags:
        valid = [t for t in tags if t in CARE_TEAM_TAGS]
        if valid:
            # de-dup, preserve order
            seen: Dict[str, None] = {}
            for t in valid:
                seen.setdefault(t, None)
            return list(seen.keys())

    member = record.get("care_team_member")
    if member and member in LEGACY_MEMBER_TO_TAG:
        return [LEGACY_MEMBER_TO_TAG[member]]

    # Untagged flagged note → doctor only (safest comprehensive default).
    return ["doctor"]


def note_visible_in_report(record: Dict[str, Any], report_type: str) -> bool:
    """Whether a flagged feedback note should appear in the given report type.

    Doctor report sees ALL flagged notes (comprehensive, preserves Beta 1.0).
    Other reports show a note only if their provider bucket is in the note's
    computed visibility set.
    """
    provider = REPORT_TYPE_TO_PROVIDER.get(report_type, "doctor")
    if provider == "doctor":
        return True
    return provider in compute_note_visibility(record)


# ── Activity relevance policy (Beta 2.0, Step 3B) ───────────────────────────
# Read-time SUGGESTION layer — which provider reports an activity is *relevant*
# to, based on domain (+ optional subdomain hints). This is distinct from parent
# note visibility above. Defined as a constant for documentation, future use, and
# tests. It does NOT change plan/activity generation and is NOT stored on
# activities. Doctor is always relevant.

# Domain key → default relevant provider buckets (besides doctor, who sees all).
DOMAIN_RELEVANCE: Dict[str, tuple] = {
    "language_and_communication": ("st",),
    "social_and_emotional": ("st", "ot_pt"),   # social communication → st; regulation/sensory → ot_pt
    "movement_and_physical": ("ot_pt",),
    "cognitive": ("st", "ot_pt"),               # language-based → st; visual-motor/attention/adaptive → ot_pt
}

# Subdomain substring hints that sharpen an ambiguous cognitive/social domain.
_ST_SUBDOMAIN_HINTS = (
    "language", "communicat", "vocab", "expressive", "receptive", "naming",
    "joint_attention", "turn", "request", "imitat", "pretend", "concept", "color", "colour",
)
_OT_PT_SUBDOMAIN_HINTS = (
    "motor", "coordination", "motor_planning", "sensory", "regulation", "adaptive",
    "self_help", "attention", "visual_motor", "sequenc", "transition", "postural", "feeding",
)


def activity_relevance_providers(domain: str, subdomain: str = "") -> List[str]:
    """Return provider buckets an activity is relevant to (doctor always included).

    Suggestion only — used for documentation/tests and any future read-time report
    sectioning. Parent note visibility (compute_note_visibility) always overrides
    relevance for whether a *note* is shown.
    """
    providers = {"doctor"}
    sub = (subdomain or "").lower()
    if domain in ("cognitive", "social_and_emotional"):
        # Disambiguate via subdomain hints; fall back to the domain default set.
        matched = False
        if any(h in sub for h in _ST_SUBDOMAIN_HINTS):
            providers.add("st"); matched = True
        if any(h in sub for h in _OT_PT_SUBDOMAIN_HINTS):
            providers.add("ot_pt"); matched = True
        if not matched:
            providers.update(DOMAIN_RELEVANCE.get(domain, ()))
    else:
        providers.update(DOMAIN_RELEVANCE.get(domain, ()))
    return [p for p in CARE_TEAM_TAGS if p in providers]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _domain_label(key: str) -> str:
    return DOMAIN_LABELS.get(key, key.replace("_", " ").title())


def _summarise_feedback(feedback_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute aggregate counts from the feedback list."""
    total       = len(feedback_list)
    completed   = sum(1 for f in feedback_list if f.get("completion") == "did_it")
    loved       = sum(1 for f in feedback_list if f.get("enjoyment") == "loved_it")
    okay        = sum(1 for f in feedback_list if f.get("enjoyment") == "it_was_okay")
    not_really  = sum(1 for f in feedback_list if f.get("enjoyment") == "not_really")
    too_easy    = sum(1 for f in feedback_list if f.get("difficulty") == "too_easy")
    just_right  = sum(1 for f in feedback_list if f.get("difficulty") == "just_right")
    too_hard    = sum(1 for f in feedback_list if f.get("difficulty") == "too_hard")
    flagged     = [f for f in feedback_list if f.get("discuss_with_care_team")]
    domains     = sorted({f.get("domain", "") for f in feedback_list if f.get("domain")})
    return {
        "total": total, "completed": completed,
        "loved": loved, "okay": okay, "not_really": not_really,
        "too_easy": too_easy, "just_right": just_right, "too_hard": too_hard,
        "flagged": flagged, "domains": domains,
    }


# ── Main generator ───────────────────────────────────────────────────────────

def generate_report_body(
    session_doc: Dict[str, Any],
    report_type: str,
) -> str:
    """
    Build a plain-text report body for the given report_type.

    Reads from session_doc:
      - age_in_months, daily_time_minutes, diagnosis_or_condition, timezone
      - plans[current_plan_id]["plan_period"] — week date range
      - plans[current_plan_id]["plan_response"] — domain and activity summary
      - feedback — practice log

    Returns a multi-line string. Never includes child name, brain_state,
    plan_internal, gate_report, or any internal debug fields.
    """
    age_months:   int  = session_doc.get("age_in_months", 0)
    daily_mins:   int  = session_doc.get("daily_time_minutes", 0)
    diagnosis:    str  = session_doc.get("diagnosis_or_condition", "") or ""
    feedback_list: List[Dict[str, Any]] = session_doc.get("feedback") or []

    # Pull plan data if available
    current_plan_id: Optional[str] = session_doc.get("current_plan_id")
    plans: Dict[str, Any] = session_doc.get("plans") or {}
    plan_entry = plans.get(current_plan_id, {}) if current_plan_id else {}
    plan_period: Dict[str, Any] = plan_entry.get("plan_period") or {}
    plan_response: Dict[str, Any] = plan_entry.get("plan_response") or {}

    # Domain labels from plan_response progress_summary
    progress = plan_response.get("progress_summary") or {}
    domains_covered: List[Dict] = progress.get("domains_covered") or []
    domain_labels: List[str] = [d.get("label", "") for d in domains_covered if d.get("label")]

    week_days: List[Dict] = plan_response.get("week") or []
    total_planned = sum(len(d.get("activities", [])) for d in week_days)
    plan_days = len(week_days)

    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    plan_start = plan_period.get("plan_start_date", "")
    plan_end   = plan_period.get("plan_end_date", "")

    lines: List[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    title = REPORT_TITLES.get(report_type, "Care Team Report")
    lines.append(title)
    lines.append("=" * len(title))
    lines.append(f"Generated: {now_str}")
    lines.append("")

    # ── Opener ──────────────────────────────────────────────────────────────
    opener = _REPORT_OPENERS.get(report_type, "")
    if opener:
        lines.append(opener)
        lines.append("")

    # ── Child profile ────────────────────────────────────────────────────────
    lines.append("Child Profile")
    lines.append("─" * 13)
    lines.append(f"Age: {age_months} months")
    if diagnosis and diagnosis not in ("No known diagnosis / not sure", "Prefer not to say"):
        lines.append(f"Diagnosis or condition: {diagnosis}")
    lines.append(f"Daily practice time: {daily_mins} minutes")
    if domain_labels:
        lines.append(f"Focus areas: {', '.join(domain_labels)}")
    lines.append("")

    # ── Plan summary ─────────────────────────────────────────────────────────
    if plan_period:
        lines.append("Weekly Plan Summary")
        lines.append("─" * 19)
        plan_type_label = "Full week" if not plan_period.get("is_partial_week") else "Partial week (mid-week start)"
        lines.append(f"Plan type: {plan_type_label}")
        if plan_start and plan_end:
            lines.append(f"Period: {plan_start} to {plan_end}")
        if total_planned > 0:
            lines.append(
                f"{_plural(total_planned, 'activity', 'activities')} planned "
                f"across {_plural(plan_days, 'day', 'days')}"
            )
        lines.append("")

    # ── Practice summary (feedback) ──────────────────────────────────────────
    lines.append("Practice Summary")
    lines.append("─" * 16)

    if not feedback_list:
        lines.append(
            "No practice data recorded yet. This section will update once the "
            "parent begins logging completed activities."
        )
    else:
        s = _summarise_feedback(feedback_list)
        lines.append(f"Activities logged: {s['total']}")
        lines.append(f"Completed (\"did it\"): {s['completed']}")

        if s["total"] > 0:
            # Enjoyment breakdown
            parts = []
            if s["loved"] > 0:   parts.append(f"{s['loved']} loved it")
            if s["okay"] > 0:    parts.append(f"{s['okay']} were okay")
            if s["not_really"] > 0: parts.append(f"{s['not_really']} not really")
            if parts:
                lines.append(f"Enjoyment: {', '.join(parts)}")

            # Difficulty breakdown
            parts = []
            if s["too_easy"]   > 0: parts.append(f"{s['too_easy']} too easy")
            if s["just_right"] > 0: parts.append(f"{s['just_right']} just right")
            if s["too_hard"]   > 0: parts.append(f"{s['too_hard']} too hard")
            if parts:
                lines.append(f"Difficulty: {', '.join(parts)}")

        if s["domains"]:
            labels = [_domain_label(d) for d in s["domains"]]
            lines.append(f"Domains practised: {', '.join(labels)}")

        # Flagged items — filtered by parent note visibility for this report type.
        # Doctor sees all; speech_therapist sees st-visible; ot_pt (and legacy
        # occupational_therapist / physical_therapist) see ot_pt-visible notes.
        visible_flagged = [
            f for f in s["flagged"] if note_visible_in_report(f, report_type)
        ]
        if visible_flagged:
            lines.append("")
            lines.append(f"Items Flagged for Care Team ({len(visible_flagged)})")
            lines.append("─" * 30)
            for f in visible_flagged:
                who  = f.get("care_team_member") or "care team"
                date = f.get("activity_date") or ""
                dom  = _domain_label(f.get("domain", ""))
                note = f.get("note", "").strip()
                fam  = f.get("activity_family", "")
                tag  = f"{dom}" + (f" / {fam}" if fam else "")
                line = f"  • {date}  [{who}]  {tag}"
                if note:
                    line += f"\n    Note: {note}"
                lines.append(line)

    lines.append("")

    # ── Disclaimer ───────────────────────────────────────────────────────────
    lines.append("─" * 60)
    lines.append(
        "This report was prepared using Genex, a home practice planning "
        "tool. It is a parent-reported summary and is not a substitute for "
        "clinical assessment. Please use your professional judgment alongside "
        "any direct evaluation."
    )

    return "\n".join(lines)
