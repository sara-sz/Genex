"""
app.py — Genex Advisor Alpha
Private Streamlit app for Sara and 1–3 advisors.
No login, no database, no cloud deployment in this version.

Run: streamlit run app.py
"""

import os
import copy
import json
from pathlib import Path
from datetime import datetime

from genex_core.storage import save_json as _storage_save

import streamlit as st

# ------------------------------------------------------------------ #
#  Page config (must be first Streamlit call)
# ------------------------------------------------------------------ #
st.set_page_config(
    page_title="Genex — Developmental Support",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ------------------------------------------------------------------ #
#  Imports from genex_core
# ------------------------------------------------------------------ #
from genex_core.config import DOMAIN_CONFIG
from genex_core.interview_engine import (
    init_state_from_profile,
    build_milestone_questions,
    record_answer,
    ensure_concern_profile,
)
from genex_core.delay_engine import estimate_all_delays
from genex_core.scoring import finalize_domain_dev_age
from genex_core.support_tiers import (
    compute_support_metrics,
    determine_family_guidance_floor,
    TIER_DISPLAY,
    TIER_COLOR,
)
from genex_core.activity_engine import generate_category_activity_bank
from genex_core.scheduler import allocate_weekly_slots, build_weekly_schedule
from genex_core.summaries import build_domain_results, build_doctor_visit_prep, get_focus_areas

# ------------------------------------------------------------------ #
#  Constants & helpers
# ------------------------------------------------------------------ #
SCREENS = [
    "welcome",
    "profile",
    "concerns",
    "interview",
    "results",
    "weekly_plan",
    "doctor_prep",
    "feedback",
]

SCREEN_LABELS = {
    "welcome":    "Welcome",
    "profile":    "Child Profile",
    "concerns":   "Concerns",
    "interview":  "Milestone Interview",
    "results":    "Results",
    "weekly_plan":"Weekly Plan",
    "doctor_prep":"Doctor Prep",
    "feedback":   "Feedback",
}

ANSWER_OPTIONS = {
    "Yes — can do this": "yes",
    "Sometimes / not always": "sometimes",
    "Only with help": "with_help",
    "Not yet": "no",
    "Not sure": "not_sure",
}

DOMAIN_ICONS = {
    "movement_and_physical": "🏃",
    "social_and_emotional": "💛",
    "language_and_communication": "💬",
    "cognitive": "🧩",
}

# On Cloud Run, SESSION_DIR env var is set to /tmp/sessions (ephemeral — GCS is primary).
# Locally it defaults to outputs/sessions/.
SESSION_DIR = Path(os.environ.get("SESSION_DIR", "outputs/sessions"))
SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _password_gate():
    """
    Phase 2B advisor access: simple shared-password gate.
    Controlled entirely by the ADVISOR_PASSWORD environment variable.
      - Not set (local dev, Phase 1, Phase 2A): gate is open, function returns immediately.
      - Set: shows a password prompt; stops rendering until the correct password is entered.
    Remove the _password_gate() call from main() when switching to Phase 2A (IAP).
    """
    required = os.environ.get("ADVISOR_PASSWORD", "").strip()
    if not required:
        return  # Gate disabled — local dev or IAP-protected deploy

    if st.session_state.get("_authenticated"):
        return  # Already authenticated this session

    st.markdown("## 🔒 Genex Advisor Alpha")
    st.caption("Enter the access password provided by Sara to continue.")
    pwd = st.text_input("Password", type="password", key="_pwd_input")
    if st.button("Enter", type="primary"):
        if pwd == required:
            st.session_state["_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password — please try again or contact Sara.")
    st.stop()


def go_to(screen: str):
    st.session_state["screen"] = screen
    st.rerun()


def current_screen() -> str:
    return st.session_state.get("screen", "welcome")


def get_state() -> dict:
    if "genex_state" not in st.session_state:
        st.session_state["genex_state"] = {}
    return st.session_state["genex_state"]


def progress_bar():
    idx = SCREENS.index(current_screen())
    pct = int((idx / (len(SCREENS) - 1)) * 100)
    st.progress(pct, text=f"Step {idx + 1} of {len(SCREENS)}: {SCREEN_LABELS[current_screen()]}")


def nav_buttons(back_screen: str = None, next_label: str = "Continue →", next_fn=None):
    col1, col2 = st.columns([1, 3])
    if back_screen:
        with col1:
            if st.button("← Back", use_container_width=True):
                go_to(back_screen)
    with col2:
        if next_fn:
            if st.button(next_label, type="primary", use_container_width=True):
                next_fn()


def restart_button(label: str = "🔄 Start a New Case"):
    """Clear all session state and return to welcome screen."""
    if st.button(label, use_container_width=True):
        for key in list(st.session_state.keys()):
            if key != "screen":
                del st.session_state[key]
        go_to("welcome")


def build_text_summary(state: dict) -> str:
    """Build a plain-text summary for advisors to copy or download."""
    from genex_core.summaries import build_domain_results, build_doctor_visit_prep
    child = state.get("child", {})
    domain_results = build_domain_results(state)
    prep = build_doctor_visit_prep(state)
    schedule = state.get("weekly_schedule", {})

    lines = [
        "=" * 60,
        "GENEX — ADVISOR SUMMARY",
        f"Generated: {datetime.now().strftime('%B %d, %Y %H:%M')}",
        "=" * 60,
        "",
        "CHILD PROFILE",
        f"  Name: {child.get('name', '—')}",
        f"  Age: {child.get('chronological_months', '—')} months",
        f"  Diagnosis / condition: {child.get('diagnosis', '—')}",
        f"  Parent concern: {child.get('concern', '—')}",
        f"  Daily time available: {child.get('daily_time_min', '—')} min/day",
        "",
        "DOMAIN RESULTS",
    ]
    for r in domain_results:
        dev_age = r.get("effective_dev_age_months", "—")
        gap = r.get("milestone_gap_months", "—")
        tier = TIER_DISPLAY.get(r.get("planning_tier", ""), r.get("planning_tier", ""))
        level = r.get("support_level", "—")
        lines.append(
            f"  {r['category']}: dev age {dev_age} mo | gap {gap} mo | "
            f"{tier} | {level}"
        )

    lines += ["", "AREAS FLAGGED FOR EXTRA SUPPORT:"]
    for r in prep["priority_domains"]:
        lines.append(f"  - {r['category']}")

    lines += ["", "AREAS TO MONITOR:"]
    for r in prep["monitor_domains"]:
        lines.append(f"  - {r['category']}")

    lines += ["", "NOTES FOR DOCTOR / SPECIALIST:"]
    for q in prep["questions_for_doctor"]:
        lines.append(f"  - {q}")

    # Weekly plan snapshot
    if schedule.get("days"):
        lines += ["", "WEEKLY PLAN SNAPSHOT:"]
        for day, info in schedule["days"].items():
            items = info.get("items", [])
            if items:
                lines.append(f"  {day}:")
                for item in items:
                    lines.append(f"    • {item['title']} ({item['duration_min']} min) — {item['category']}")

    lines += [
        "",
        "=" * 60,
        prep["disclaimer"],
        "=" * 60,
    ]
    return "\n".join(lines)


def _build_flat_activity_bank(state: dict) -> list:
    """Return a flat sorted list of all activities across all domain banks for the add-activity selector."""
    activities = []
    for category_key, cfg in DOMAIN_CONFIG.items():
        bank = state.get("activity_banks", {}).get(category_key, {})
        for a in bank.get("activities", []):
            activities.append({
                "category_key": category_key,
                "category": cfg["display"],
                "title": a.get("title", "Activity"),
                "duration_min": int(a.get("duration_min", 5)),
                "instructions": a.get("instructions", ""),
                "materials": a.get("materials", ""),
                "goal": a.get("goal", ""),
                "level": a.get("level", "current_or_next"),
            })
    return sorted(activities, key=lambda x: (x["category"], x["duration_min"]))


def _build_plan_text(state: dict, editable_plan: dict) -> str:
    """Build a printable/downloadable plain-text version of the confirmed weekly plan."""
    child = state.get("child", {})
    lines = [
        "=" * 60,
        "GENEX — CONFIRMED WEEKLY HOME-SUPPORT PLAN",
        f"Child: {child.get('name', '—')} · {child.get('chronological_months', '—')} months",
        f"Generated: {datetime.now().strftime('%B %d, %Y')}",
        "=" * 60,
        "",
    ]
    for day_name, day_info in editable_plan.items():
        items = day_info.get("items", [])
        total = sum(item.get("duration_min", 0) for item in items)
        weekend_label = " 🌿 Weekend" if day_info.get("is_weekend") else ""
        lines.append(f"{day_name}{weekend_label} — {total} min")
        lines.append("-" * 40)
        if not items:
            lines.append("  Rest day — no activities scheduled.")
        else:
            for item in items:
                lines.append(f"  • {item['title']} ({item['duration_min']} min) — {item['category']}")
                if item.get("goal"):
                    lines.append(f"    Goal: {item['goal']}")
                if item.get("instructions"):
                    lines.append(f"    {item['instructions']}")
                if item.get("materials"):
                    lines.append(f"    Materials: {item['materials']}")
        lines.append("")

    lines += [
        "=" * 60,
        "This plan was created by Genex. It is not a clinical prescription.",
        "Please review with your child's care team before starting.",
        "=" * 60,
    ]
    return "\n".join(lines)


def tier_badge(tier: str) -> str:
    color = TIER_COLOR.get(tier, "#aaa")
    label = TIER_DISPLAY.get(tier, tier)
    return f'<span style="background:{color};color:white;padding:3px 10px;border-radius:12px;font-size:0.85em;font-weight:600">{label}</span>'


def save_session_json(state: dict):
    """
    Save a de-identified session snapshot.
    Writes to GCS when GCS_BUCKET env var is set (Cloud Run),
    otherwise falls back to SESSION_DIR on the local filesystem.
    Never raises — a save failure must not break the UI.
    """
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        child = state.get("child", {})
        fname = f"{child.get('name', 'child')}_{stamp}.json"
        snapshot = {
            "child": child,
            "delay_estimates": state.get("delay_estimates", {}),
            "dev_age": state.get("dev_age", {}),
            "domain_results": build_domain_results(state),
            "generated_at": stamp,
        }
        _storage_save(snapshot, f"sessions/{fname}", SESSION_DIR)
    except Exception:
        pass  # Never break the UI for a save failure


# ------------------------------------------------------------------ #
#  Screen 1 — Welcome + Disclaimer
# ------------------------------------------------------------------ #
def screen_welcome():
    st.markdown("## 🌱 Welcome to Genex")
    st.markdown("**Developmental Support for Families**")
    st.divider()

    st.markdown("""
This private Genex alpha helps us understand a child's developmental profile and
create a practical home-support plan in under 10 minutes.

**What Genex does:**
- Asks structured milestone questions across four developmental domains
- Estimates where your child is developmentally in each area
- Suggests home activities matched to your child's current level
- Builds a realistic weekly plan based on your available time
- Generates notes to help prepare for doctor or specialist visits

---
""")

    st.warning(
        "⚠️ **Important Disclaimer** \n\n"
        "Genex is a **support tool, not a diagnostic or clinical tool**. "
        "It does not replace assessment or advice from a pediatrician, developmental pediatrician, "
        "speech-language pathologist, occupational therapist, or any other qualified specialist. "
        "Results are based on parent-reported answers and should be reviewed together with your child's care team.\n\n"
        "**Privacy:** This tool collects only a first name and age in months — no full name, "
        "date of birth, address, school, doctor name, or identifying documents."
    )

    st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button("I understand — Get Started", type="primary", use_container_width=True):
            go_to("profile")


# ------------------------------------------------------------------ #
#  Screen 2 — Child Profile Form
# ------------------------------------------------------------------ #
def screen_profile():
    progress_bar()
    st.markdown("## 👶 Child Profile")
    st.caption(
        "We collect only what's needed to select the right questions. "
        "No last name, date of birth, or identifying information."
    )
    st.divider()

    with st.form("profile_form"):
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input(
                "Child's first name (or nickname)",
                value=st.session_state.get("pf_name", ""),
                placeholder="e.g. Emma",
                help="First name or nickname only — no last name.",
            )
        with col2:
            age_months = st.number_input(
                "Age in months",
                min_value=1, max_value=72,
                value=st.session_state.get("pf_age", 24),
                help="Enter age in months, e.g. 18, 24, 36, 48, 60.",
            )

        daily_time_min = st.slider(
            "How many minutes per day can you usually spend on activities with your child?",
            min_value=5, max_value=60, step=5,
            value=st.session_state.get("pf_time", 15),
            help="Be realistic — 10 minutes done consistently is better than 30 minutes occasionally.",
        )

        submitted = st.form_submit_button("Save Profile →", type="primary")

    if submitted:
        if not name.strip():
            st.error("Please enter a name for your child.")
            return
        st.session_state["pf_name"] = name.strip()
        st.session_state["pf_age"] = int(age_months)
        st.session_state["pf_time"] = int(daily_time_min)
        go_to("concerns")

    nav_buttons(back_screen="welcome")


# ------------------------------------------------------------------ #
#  Screen 3 — Parent Concern / Condition Form
# ------------------------------------------------------------------ #
def screen_concerns():
    progress_bar()
    st.markdown("## 💬 Concerns & Background")
    st.divider()

    name = st.session_state.get("pf_name", "your child")

    with st.form("concerns_form"):
        diagnosis = st.text_input(
            f"Has {name} received any diagnosis or condition? (Type 'none' if not)",
            value=st.session_state.get("pf_diagnosis", ""),
            placeholder="e.g. Down syndrome, speech delay, autism, no diagnosis",
        )

        concern = st.text_area(
            f"What are your main concerns about {name}'s development right now?",
            value=st.session_state.get("pf_concern", ""),
            height=140,
            placeholder=(
                "e.g. Not walking yet, very few words, difficulty with transitions, "
                "not making eye contact, feeding issues, short attention span..."
            ),
            help="Be as specific as you like — this helps the tool focus on the most relevant milestones.",
        )

        submitted = st.form_submit_button("Continue to Interview →", type="primary")

    if submitted:
        if not concern.strip():
            st.error("Please describe your main concerns — even briefly.")
            return

        st.session_state["pf_diagnosis"] = diagnosis.strip() or "None reported"
        st.session_state["pf_concern"] = concern.strip()

        # Initialize state
        with st.spinner("Setting up the interview…"):
            state = init_state_from_profile(
                name=st.session_state["pf_name"],
                chronological_months=st.session_state["pf_age"],
                diagnosis=st.session_state["pf_diagnosis"],
                concern=st.session_state["pf_concern"],
                daily_time_min=st.session_state["pf_time"],
            )
            # Estimate delays (AI-assisted or fallback)
            estimate_all_delays(state)
            st.session_state["genex_state"] = state
            st.session_state["interview_domain_idx"] = 0
            st.session_state["interview_questions"] = {}

        go_to("interview")

    nav_buttons(back_screen="profile")


# ------------------------------------------------------------------ #
#  Screen 4 — Milestone Interview (adaptive: one band at a time)
# ------------------------------------------------------------------ #

def _band_score(questions: list, responses: dict) -> float:
    """Average answer score for a band. Pass threshold: >= 0.5."""
    from genex_core.interview_engine import normalize_answer, score_answer
    if not questions:
        return 0.0
    scores = [
        score_answer(normalize_answer(
            ANSWER_OPTIONS.get(responses.get(q["question_id"], "Not yet"), "no")
        ))
        for q in questions
    ]
    return sum(scores) / len(scores)


def _clear_domain_band_state(category_key: str):
    """Remove adaptive interview session state for one domain."""
    for k in [f"bq_{category_key}", f"bm_{category_key}",
              f"bi_{category_key}", f"bf_{category_key}"]:
        st.session_state.pop(k, None)


def _advance_domain(state: dict, category_key: str, domain_idx: int):
    """Finalise current domain and move to the next one."""
    _clear_domain_band_state(category_key)
    st.session_state["genex_state"] = state
    st.session_state["interview_domain_idx"] = domain_idx + 1
    st.rerun()


def screen_interview():
    progress_bar()

    state = get_state()
    if not state:
        st.warning("Please start from the beginning.")
        if st.button("← Start Over"):
            go_to("welcome")
        return

    domain_keys = list(DOMAIN_CONFIG.keys())
    domain_idx = st.session_state.get("interview_domain_idx", 0)

    # ── All domains done → finalise and go to results ───────────────────────
    if domain_idx >= len(domain_keys):
        with st.spinner("Calculating results…"):
            for dk in domain_keys:
                finalize_domain_dev_age(state, dk)
            determine_family_guidance_floor(state)
            save_session_json(state)
        go_to("results")
        return

    category_key = domain_keys[domain_idx]
    category_display = DOMAIN_CONFIG[category_key]["display"]
    icon = DOMAIN_ICONS.get(category_key, "📋")
    child_name = state["child"]["name"]

    # ── Initialise band state for this domain ───────────────────────────────
    if f"bq_{category_key}" not in st.session_state:
        all_qs = build_milestone_questions(state, category_key)
        bands: dict = {}
        for q in all_qs:
            bands.setdefault(q["months"], []).append(q)
        band_months = sorted(bands.keys())

        if not band_months:
            # No questions — skip domain silently
            if category_key not in state["qna"]:
                state["qna"][category_key] = []
            _advance_domain(state, category_key, domain_idx)
            return

        st.session_state[f"bq_{category_key}"] = bands
        st.session_state[f"bm_{category_key}"] = band_months
        st.session_state[f"bi_{category_key}"] = 0
        st.session_state[f"bf_{category_key}"] = 0
        if category_key not in state["qna"]:
            state["qna"][category_key] = []

    bands        = st.session_state[f"bq_{category_key}"]
    band_months  = st.session_state[f"bm_{category_key}"]
    band_idx     = st.session_state[f"bi_{category_key}"]
    consec_fails = st.session_state[f"bf_{category_key}"]

    # Band index past the end → domain done
    if band_idx >= len(band_months):
        _advance_domain(state, category_key, domain_idx)
        return

    current_month = band_months[band_idx]
    questions     = bands[current_month]

    # ── Header ───────────────────────────────────────────────────────────────
    st.markdown(f"## {icon} Milestone Interview")
    st.markdown(
        f"**Domain {domain_idx + 1} of {len(domain_keys)}: {category_display}**  \n"
        f"Question group {band_idx + 1} of up to {len(band_months)} "
        f"— around **{current_month} months**"
    )

    # Visual band-progress bar (within this domain)
    band_pct = int((band_idx / max(len(band_months), 1)) * 100)
    st.progress(band_pct)

    remaining_bands = len(band_months) - band_idx - 1
    st.caption(
        f"Answer based on what **{child_name}** can do right now, at home. "
        + (f"Up to {remaining_bands} more group(s) to go in this domain." if remaining_bands > 0
           else "This is the last group for this domain.")
    )
    st.divider()

    # ── Answer form for this band ────────────────────────────────────────────
    # Cache previously selected indices so back-navigation restores choices
    cache_key = f"bc_{category_key}_{current_month}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = {}

    with st.form(f"band_form_{category_key}_{current_month}"):
        responses = {}
        for q in questions:
            subdomain_hint = q.get("subdomain", "").replace("_", " ")
            sel = st.radio(
                f"Can **{child_name}** {q['milestone']}?",
                options=list(ANSWER_OPTIONS.keys()),
                index=st.session_state[cache_key].get(q["question_id"], 0),
                key=f"bq_{q['question_id']}",
                help=f"Area: {subdomain_hint}" if subdomain_hint not in ("", "unspecified") else None,
                horizontal=True,
            )
            responses[q["question_id"]] = sel

        submitted = st.form_submit_button(
            "Save & continue →",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        # Persist selection indices for possible back navigation
        for qid, sel in responses.items():
            st.session_state[cache_key][qid] = list(ANSWER_OPTIONS.keys()).index(sel)

        # Record answers into qna state
        for q in questions:
            record_answer(state, category_key, q, ANSWER_OPTIONS[responses[q["question_id"]]])

        st.session_state["genex_state"] = state

        # ── Adaptive stopping logic ──────────────────────────────────────────
        # Pass = average score >= 0.5  |  Fail = average score < 0.5
        # Rule: 2 consecutive fails → stop asking higher bands for this domain
        score = _band_score(questions, responses)
        passed = score >= 0.5

        if passed:
            st.session_state[f"bf_{category_key}"] = 0        # reset fail streak
        else:
            st.session_state[f"bf_{category_key}"] = consec_fails + 1

        updated_fails = st.session_state[f"bf_{category_key}"]
        next_idx      = band_idx + 1

        # Stop if: 2 consecutive fails, OR no more bands left
        if updated_fails >= 2 or next_idx >= len(band_months):
            _advance_domain(state, category_key, domain_idx)
        else:
            st.session_state[f"bi_{category_key}"] = next_idx
            st.rerun()

    # ── Navigation ───────────────────────────────────────────────────────────
    st.divider()
    col_back, _ = st.columns([1, 3])
    with col_back:
        if band_idx > 0:
            # Go back one band within the same domain
            if st.button(f"← Back to {band_months[band_idx - 1]}m group"):
                # Remove this band's already-recorded answers
                n = len(bands[band_months[band_idx - 1]])
                if state["qna"].get(category_key):
                    state["qna"][category_key] = state["qna"][category_key][:-n]
                st.session_state["genex_state"] = state
                st.session_state[f"bi_{category_key}"] = band_idx - 1
                st.session_state[f"bf_{category_key}"] = 0
                st.rerun()

        elif domain_idx > 0:
            # Go back to the previous domain (restart it from band 0)
            if st.button("← Previous Domain"):
                prev_key = domain_keys[domain_idx - 1]
                # Clear current domain's progress
                _clear_domain_band_state(category_key)
                state["qna"][category_key] = []
                # Clear previous domain's progress so it restarts
                _clear_domain_band_state(prev_key)
                state["qna"][prev_key] = []
                st.session_state["genex_state"] = state
                st.session_state["interview_domain_idx"] = domain_idx - 1
                st.rerun()


# ------------------------------------------------------------------ #
#  Screen 5 — Results by Domain
# ------------------------------------------------------------------ #
def screen_results():
    progress_bar()
    st.markdown("## 📊 Results by Domain")

    state = get_state()
    if not state.get("dev_age"):
        st.warning("No results yet. Please complete the interview first.")
        return

    child = state["child"]
    domain_results = build_domain_results(state)
    chrono = child["chronological_months"]

    st.markdown(
        f"**{child['name']}** · {chrono} months old · {child.get('diagnosis', '')}",
    )
    st.divider()

    # Column header row
    hcol1, hcol2, hcol3, hcol4 = st.columns([3, 2, 2, 3])
    with hcol1:
        st.markdown("<span style='font-size:0.8em;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:0.05em'>Domain</span>", unsafe_allow_html=True)
    with hcol2:
        st.markdown("<span style='font-size:0.8em;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:0.05em'>Est. Dev Age</span>", unsafe_allow_html=True)
    with hcol3:
        st.markdown("<span style='font-size:0.8em;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:0.05em'>Gap</span>", unsafe_allow_html=True)
    with hcol4:
        st.markdown("<span style='font-size:0.8em;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:0.05em'>Support Level</span>", unsafe_allow_html=True)

    for r in domain_results:
        with st.container():
            icon = DOMAIN_ICONS.get(r["category_key"], "📋")
            col1, col2, col3, col4 = st.columns([3, 2, 2, 3])

            with col1:
                st.markdown(f"**{icon} {r['category']}**")
                st.markdown(
                    tier_badge(r["planning_tier"]),
                    unsafe_allow_html=True,
                )

            with col2:
                dev_age = r.get("effective_dev_age_months")
                val = f"{dev_age} mo" if dev_age is not None else "—"
                st.markdown(f"<span style='font-size:1em;font-weight:500'>{val}</span>", unsafe_allow_html=True)

            with col3:
                gap = r.get("milestone_gap_months")
                val = f"{gap} mo" if gap is not None else "—"
                st.markdown(f"<span style='font-size:1em;font-weight:500'>{val}</span>", unsafe_allow_html=True)

            with col4:
                level = r.get("support_level", "—")
                st.markdown(f"<span style='font-size:1em;font-weight:500'>{level}</span>", unsafe_allow_html=True)

        st.divider()

    # Show QnA transcript in expander
    with st.expander("View full Q&A transcript"):
        for category_key, cfg in DOMAIN_CONFIG.items():
            answers = state.get("qna", {}).get(category_key, [])
            if answers:
                st.markdown(f"**{cfg['display']}**")
                for a in answers:
                    emoji = {"yes": "✅", "sometimes": "🔶", "with_help": "🤝", "no": "❌", "not_sure": "❓"}.get(
                        a.get("norm_answer", ""), "❓"
                    )
                    st.markdown(
                        f"{emoji} *{a['milestone']}* ({a['months']}m) — **{a['norm_answer'].replace('_', ' ')}**"
                    )
                st.markdown("")

    st.divider()

    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("← Back to Interview", use_container_width=True):
            # Restart the last domain from band 0
            last_key = list(DOMAIN_CONFIG.keys())[-1]
            _clear_domain_band_state(last_key)
            state["qna"][last_key] = []
            st.session_state["genex_state"] = state
            st.session_state["interview_domain_idx"] = len(DOMAIN_CONFIG) - 1
            go_to("interview")
    with col2:
        if st.button("Generate Weekly Plan →", type="primary", use_container_width=True):
            with st.spinner("Generating activities and weekly plan…"):
                determine_family_guidance_floor(state)
                for category_key in DOMAIN_CONFIG:
                    generate_category_activity_bank(state, category_key)
                allocate_weekly_slots(state)
                build_weekly_schedule(state)
                st.session_state["genex_state"] = state
                # Reset the editable plan so the new schedule is picked up fresh
                st.session_state.pop("editable_plan", None)
                st.session_state.pop("plan_confirmed", None)
            go_to("weekly_plan")
    with col3:
        restart_button()


# ------------------------------------------------------------------ #
#  Screen 6 — Weekly Home-Support Plan (editable)
# ------------------------------------------------------------------ #
def _render_activity_item(item: dict, day_name: str, idx: int, confirmed: bool):
    """Render one activity card with optional delete button."""
    icon = DOMAIN_ICONS.get(item.get("category_key", ""), "📋")
    col_content, col_del = st.columns([11, 1])
    with col_content:
        st.markdown(
            f"**{icon} {item['title']}** &nbsp;·&nbsp; "
            f"<span style='color:#888'>{item['duration_min']} min · {item['category']}</span>",
            unsafe_allow_html=True,
        )
        if item.get("goal"):
            goal_raw = item["goal"]
            goal_label = TIER_DISPLAY.get(goal_raw, goal_raw)
            st.markdown(
                f"<span style='font-size:0.85em;color:#555'>🎯 <em>{goal_label}</em></span>",
                unsafe_allow_html=True,
            )
        if item.get("instructions"):
            st.markdown(item["instructions"])
        if item.get("materials"):
            st.caption(f"🧰 Materials: {item['materials']}")
    with col_del:
        if not confirmed:
            if st.button("🗑️", key=f"del_{day_name}_{idx}", help="Remove this activity"):
                st.session_state["editable_plan"][day_name]["items"].pop(idx)
                st.session_state["plan_confirmed"] = False
                st.rerun()
    st.markdown("---")


def screen_weekly_plan():
    progress_bar()
    st.markdown("## 📅 Weekly Home-Support Plan")

    state = get_state()
    if not state.get("weekly_schedule"):
        st.warning("No plan generated yet.")
        if st.button("← Back to Results"):
            go_to("results")
        return

    child = state["child"]
    schedule = state["weekly_schedule"]

    # ── Initialise editable plan from generated schedule (once per generation) ──
    if "editable_plan" not in st.session_state:
        st.session_state["editable_plan"] = copy.deepcopy(schedule.get("days", {}))
        st.session_state["plan_confirmed"] = False

    editable_plan: dict = st.session_state["editable_plan"]
    plan_confirmed: bool = st.session_state.get("plan_confirmed", False)

    # ── Build flat activity bank for the add-activity selector ──────────────
    activity_bank_flat = _build_flat_activity_bank(state)
    activity_options = {
        f"{a['title']} ({a['duration_min']} min) — {a['category']}": a
        for a in activity_bank_flat
    }

    # ── Status bar ──────────────────────────────────────────────────────────
    st.caption(
        f"**{child['name']}** · {child['daily_time_min']} min/day weekdays · "
        f"Weekend: longer & playdate-type activities"
    )

    if schedule.get("status") == "no_special_support":
        st.success(
            "🎉 Based on the interview, no special support activities are needed right now. "
            "Keep up the everyday play and interaction!"
        )

    if schedule.get("summary") and schedule.get("status") != "no_special_support":
        st.info(schedule["summary"])

    if plan_confirmed:
        st.success("✅ Plan confirmed — download or print below, then continue to Doctor Visit Prep.")
    else:
        st.info(
            "Review the plan below. Use 🗑️ to remove any activity that doesn't fit. "
            "Use **➕ Add activity** to swap in something from the activity bank. "
            "When you're happy, click **Confirm Plan**."
        )

    st.divider()

    # ── Day-by-day display ───────────────────────────────────────────────────
    for day_name, day_info in editable_plan.items():
        items = day_info.get("items", [])
        # Recalculate total in case items were added/deleted
        total = sum(item.get("duration_min", 0) for item in items)
        day_info["total_minutes"] = total

        is_weekend = day_info.get("is_weekend", day_name in ("Saturday", "Sunday"))
        weekend_label = " 🌿" if is_weekend else ""
        expand_by_default = (day_name == "Monday") and not plan_confirmed

        with st.expander(
            f"**{day_name}{weekend_label}** — {total} min"
            + (" · *Weekend enrichment*" if is_weekend else ""),
            expanded=expand_by_default,
        ):
            if not items:
                st.caption("No activities scheduled. Add one below or leave as a rest day.")
            else:
                for idx, item in enumerate(items):
                    _render_activity_item(item, day_name, idx, plan_confirmed)

            # ── Add-activity panel (hidden when plan is confirmed) ────────────
            if not plan_confirmed and activity_bank_flat:
                with st.expander(f"➕ Add activity to {day_name}"):
                    st.caption("Click **+ Add** next to any activity to add it to this day.")
                    for a_idx, activity in enumerate(activity_bank_flat):
                        icon_a = DOMAIN_ICONS.get(activity.get("category_key", ""), "📋")
                        snippet = activity.get("instructions", "")
                        # Trim to ~120 chars for the preview line
                        if len(snippet) > 120:
                            snippet = snippet[:117].rsplit(" ", 1)[0] + "…"
                        col_info, col_btn = st.columns([8, 1])
                        with col_info:
                            st.markdown(
                                f"{icon_a} **{activity['title']}** "
                                f"<span style='color:#888;font-size:0.85em'>"
                                f"· {activity['duration_min']} min · {activity['category']}"
                                f"</span>",
                                unsafe_allow_html=True,
                            )
                            if snippet:
                                st.markdown(
                                    f"<span style='font-size:0.82em;color:#666'>{snippet}</span>",
                                    unsafe_allow_html=True,
                                )
                        with col_btn:
                            if st.button(
                                "+ Add",
                                key=f"add_{day_name}_{a_idx}",
                                use_container_width=True,
                            ):
                                editable_plan[day_name]["items"].append(copy.deepcopy(activity))
                                st.session_state["plan_confirmed"] = False
                                st.rerun()
                        st.markdown(
                            "<hr style='margin:4px 0;border:none;border-top:1px solid #eee'>",
                            unsafe_allow_html=True,
                        )

    st.divider()

    # ── Action buttons ───────────────────────────────────────────────────────
    if not plan_confirmed:
        col1, col2 = st.columns([1, 2])
        with col1:
            if st.button("← Back to Results", use_container_width=True):
                go_to("results")
        with col2:
            if st.button("✅ Confirm This Plan", type="primary", use_container_width=True):
                st.session_state["plan_confirmed"] = True
                st.rerun()
    else:
        plan_text = _build_plan_text(state, editable_plan)
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            if st.button("✏️ Edit Plan", use_container_width=True):
                st.session_state["plan_confirmed"] = False
                st.rerun()
        with col2:
            st.download_button(
                label="⬇️ Download Plan",
                data=plan_text,
                file_name=f"genex_plan_{child['name']}_{datetime.now().strftime('%Y%m%d')}.txt",
                mime="text/plain",
                use_container_width=True,
            )
        with col3:
            if st.button("Doctor Visit Prep →", type="primary", use_container_width=True):
                go_to("doctor_prep")


# ------------------------------------------------------------------ #
#  Screen 7 — Doctor-Visit Prep
# ------------------------------------------------------------------ #
def screen_doctor_prep():
    progress_bar()
    st.markdown("## 🩺 Doctor-Visit Prep Notes")
    st.caption(
        "These notes are for your own reference — to help you communicate clearly "
        "with your child's doctor or specialist."
    )

    state = get_state()
    if not state.get("dev_age"):
        st.warning("Please complete the interview first.")
        return

    prep = build_doctor_visit_prep(state)
    child = state["child"]

    st.markdown(f"**Child:** {prep['child_name']} · {prep['chronological_months']} months")
    st.markdown(f"**Diagnosis / condition noted:** {prep['diagnosis']}")
    st.markdown(f"**Parent concern summary:** {prep['parent_concern_summary']}")

    st.divider()

    if prep["priority_domains"]:
        st.markdown("### ⚠️ Areas Where Extra Support May Help")
        for r in prep["priority_domains"]:
            st.markdown(
                f"- **{r['category']}**: parent answers suggest this may be an area where extra "
                f"support could help. Consider discussing this with the child's pediatrician, "
                f"developmental specialist, or therapist."
            )

    if prep["monitor_domains"]:
        st.markdown("### 🔍 Areas Worth Keeping an Eye On")
        for r in prep["monitor_domains"]:
            st.markdown(
                f"- **{r['category']}**: worth mentioning at the next well-child visit."
            )

    if prep["on_track_domains"]:
        st.markdown("### ✅ Areas That Appear On Track")
        for r in prep["on_track_domains"]:
            st.markdown(f"- **{r['category']}**")

    st.divider()

    if prep["milestone_qa_highlights"]:
        st.markdown("### 📋 Milestone Items Not Yet Achieved")
        st.caption("These may be worth mentioning to your doctor.")
        for item in prep["milestone_qa_highlights"]:
            st.markdown(
                f"- *{item['domain']}* ({item['months_expected']}m): "
                f"**{item['milestone']}** — answered *{item['answer'].replace('_', ' ')}*"
            )

    st.divider()

    st.markdown("### ❓ Questions to Ask Your Doctor or Specialist")
    for q in prep["questions_for_doctor"]:
        st.markdown(f"- {q}")

    st.divider()
    st.warning(prep["disclaimer"])

    st.divider()

    # Download full advisor summary
    summary_text = build_text_summary(state)
    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        st.download_button(
            label="⬇️ Download Full Advisor Summary (.txt)",
            data=summary_text,
            file_name=f"genex_summary_{child['name']}_{datetime.now().strftime('%Y%m%d')}.txt",
            mime="text/plain",
            use_container_width=True,
        )
    with col_dl2:
        with st.expander("📄 Copy-friendly text version"):
            st.text(summary_text)

    st.divider()
    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("← Back to Weekly Plan", use_container_width=True):
            go_to("weekly_plan")
    with col2:
        if st.button("Advisor Feedback →", type="primary", use_container_width=True):
            go_to("feedback")
    with col3:
        restart_button()


# ------------------------------------------------------------------ #
#  Screen 8 — Feedback Form
# ------------------------------------------------------------------ #
def screen_feedback():
    progress_bar()
    st.markdown("## 📝 Advisor Feedback")
    st.caption(
        "This is the alpha version of Genex. Your feedback directly improves the product."
    )
    st.divider()

    state = get_state()
    child = state.get("child", {})

    with st.form("feedback_form"):
        st.markdown("**Rate this output (1 = poor, 5 = excellent)**")

        col1, col2 = st.columns(2)
        with col1:
            clinical = st.slider("Clinical appropriateness", 1, 5, 3)
            safety = st.slider("Safety of suggested activities", 1, 5, 3)
            practicality = st.slider("Practicality for parents at home", 1, 5, 3)
        with col2:
            clarity = st.slider("Clarity of wording", 1, 5, 3)
            usefulness = st.slider("Overall usefulness", 1, 5, 3)
            domain_accuracy = st.slider("Accuracy of domain tier assignments", 1, 5, 3)

        st.markdown("**Short feedback**")
        what_change = st.text_area("What would you change or improve?", height=80)
        what_missing = st.text_area("What is missing?", height=80)
        concerns = st.text_area("Any concerns or red flags?", height=80)
        general = st.text_area("Any other comments?", height=80)

        submitted = st.form_submit_button("Submit Feedback", type="primary")

    if submitted:
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            feedback = {
                "child_name": child.get("name", "unknown"),
                "age_months": child.get("chronological_months"),
                "diagnosis": child.get("diagnosis"),
                "ratings": {
                    "clinical_appropriateness": clinical,
                    "safety": safety,
                    "practicality": practicality,
                    "clarity": clarity,
                    "overall_usefulness": usefulness,
                    "domain_accuracy": domain_accuracy,
                },
                "comments": {
                    "what_to_change": what_change,
                    "what_is_missing": what_missing,
                    "concerns": concerns,
                    "general": general,
                },
                "submitted_at": stamp,
            }
            fname = f"feedback_{child.get('name', 'advisor')}_{stamp}.json"
            result = _storage_save(feedback, f"feedback/{fname}", SESSION_DIR)
            if result in ("gcs", "local"):
                st.success("✅ Thank you — feedback saved!")
            else:
                st.warning("Feedback could not be saved automatically. Please copy the summary using the button above.")
        except Exception as e:
            st.error(f"Could not save feedback: {e}")

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Back to Doctor Prep", use_container_width=True):
            go_to("doctor_prep")
    with col2:
        restart_button()


# ------------------------------------------------------------------ #
#  Sidebar — quick nav for advisors
# ------------------------------------------------------------------ #
def sidebar_nav():
    with st.sidebar:
        st.markdown("### 🌱 Genex Alpha")
        st.caption("Advisor Navigation")
        for s in SCREENS:
            label = SCREEN_LABELS[s]
            if st.button(label, key=f"nav_{s}", use_container_width=True):
                go_to(s)

        state = get_state()
        if state.get("child"):
            child = state["child"]
            st.divider()
            st.caption(
                f"**{child.get('name', '—')}** · {child.get('chronological_months', '—')} mo\n\n"
                f"{child.get('diagnosis', '')}"
            )


# ------------------------------------------------------------------ #
#  Router
# ------------------------------------------------------------------ #
def main():
    if "screen" not in st.session_state:
        st.session_state["screen"] = "welcome"

    _password_gate()   # Phase 2B: no-op unless ADVISOR_PASSWORD env var is set
    sidebar_nav()

    screen = current_screen()

    if screen == "welcome":
        screen_welcome()
    elif screen == "profile":
        screen_profile()
    elif screen == "concerns":
        screen_concerns()
    elif screen == "interview":
        screen_interview()
    elif screen == "results":
        screen_results()
    elif screen == "weekly_plan":
        screen_weekly_plan()
    elif screen == "doctor_prep":
        screen_doctor_prep()
    elif screen == "feedback":
        screen_feedback()
    else:
        screen_welcome()


if __name__ == "__main__":
    main()
