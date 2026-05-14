"""
app.py — Genex Parent Copilot v0.2
Parent-led developmental home-support planning app.
Private pilot: 5 families, shared password, no accounts.

Run: streamlit run app.py
"""

import os
import copy
import json
import base64
from pathlib import Path
from datetime import datetime

import streamlit as st

# ── Page config (must be first Streamlit call) ─────────────────────────────
st.set_page_config(
    page_title="Genex — Parent Copilot",
    page_icon="🌱",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ── Load CSS (cached so it doesn't re-read on every rerun) ────────────────
@st.cache_data(show_spinner=False)
def _load_css() -> str:
    p = Path(__file__).parent / "assets" / "style.css"
    return p.read_text() if p.exists() else ""

st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)

# ── Imports from genex_core ────────────────────────────────────────────────
from genex_core.storage import save_json as _storage_save
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
)
from genex_core.activity_engine import generate_category_activity_bank
from genex_core.scheduler import allocate_weekly_slots, build_weekly_schedule
from genex_core.summaries import build_domain_results, build_doctor_visit_prep

# Pre-warm the CDC milestone table once at startup so the first interview
# page doesn't pay the Excel-load cost. Uses Streamlit's resource cache
# which persists across reruns for the lifetime of the server process.
@st.cache_resource(show_spinner=False)
def _prewarm_milestones():
    from genex_core.milestones import get_cdc_df
    get_cdc_df()

_prewarm_milestones()

# ── Constants ──────────────────────────────────────────────────────────────
SCREENS = [
    "welcome",
    "profile",
    "interview",
    "weekly_plan",
    "doctor_prep",
    "feedback",
]

SCREEN_LABELS = {
    "welcome":     "Welcome",
    "profile":     "Your Child",
    "interview":   "Quick Questions",
    "weekly_plan": "Weekly Plan",
    "doctor_prep": "Doctor Notes",
    "feedback":    "Share Feedback",
}

# Parent-friendly answer labels → internal genex_core values
ANSWER_OPTIONS = {
    "Yes, usually":    "yes",
    "Sometimes":       "sometimes",
    "Only with help":  "with_help",
    "Not yet":         "no",
    "Not sure":        "not_sure",
}

# Parent-facing domain labels
DOMAIN_LABELS = {
    "movement_and_physical":    "Moving and Playing",
    "language_and_communication": "Talking and Communicating",
    "social_and_emotional":     "Connecting and Feeling",
    "cognitive":                "Learning and Exploring",
}

DOMAIN_ICONS = {
    "movement_and_physical":    "🏃",
    "language_and_communication": "💬",
    "social_and_emotional":     "💛",
    "cognitive":                "🧩",
}

SESSION_DIR = Path(os.environ.get("SESSION_DIR", "outputs/sessions"))
SESSION_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_logo_b64() -> str | None:
    logo_path = Path(__file__).parent / "assets" / "logo.png"
    if logo_path.exists():
        return base64.b64encode(logo_path.read_bytes()).decode()
    return None


def _render_logo(height: int = 48):
    b64 = _load_logo_b64()
    if b64:
        st.markdown(
            f"<img src='data:image/png;base64,{b64}' height='{height}' "
            f"style='margin-bottom:0.5rem' alt='Genex'>",
            unsafe_allow_html=True,
        )


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
    screens_with_progress = [s for s in SCREENS if s != "welcome"]
    screen = current_screen()
    if screen == "welcome":
        return
    try:
        idx = screens_with_progress.index(screen)
    except ValueError:
        idx = 0
    pct = int(((idx + 1) / len(screens_with_progress)) * 100)
    label = SCREEN_LABELS.get(screen, "")
    st.progress(pct, text=f"**{label}** — Step {idx + 1} of {len(screens_with_progress)}")
    st.markdown("")


def restart_button():
    if st.button("🔄 Start Over", use_container_width=True):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()


def save_session_json(state: dict):
    """Save a de-identified session snapshot. Never raises."""
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        child = state.get("child", {})
        fname = f"{child.get('name', 'child')}_{stamp}.json"
        snapshot = {
            "child": {
                "name": child.get("name"),
                "chronological_months": child.get("chronological_months"),
                "diagnosis": child.get("diagnosis"),
            },
            "dev_age": state.get("dev_age", {}),
            "generated_at": stamp,
            "app_version": "parent-copilot-v0.2",
        }
        _storage_save(snapshot, f"sessions/{fname}", SESSION_DIR)
    except Exception:
        pass


# ── Password gate ──────────────────────────────────────────────────────────

def _password_gate():
    """
    Parent Copilot pilot access gate.
    Reads PARENT_PASSWORD env var (from Secret Manager on Cloud Run).
    No-op when env var is not set (local dev).
    """
    required = os.environ.get("PARENT_PASSWORD", "").strip()
    if not required:
        return
    if st.session_state.get("_authenticated"):
        return

    _render_logo(height=44)
    st.markdown("## Welcome to Genex")
    st.markdown("Please enter the access code to continue.")
    pwd = st.text_input("Access code", type="password", key="_pwd_input",
                        placeholder="Enter your access code")
    if st.button("Continue →", type="primary", use_container_width=True):
        if pwd == required:
            st.session_state["_authenticated"] = True
            st.rerun()
        else:
            st.error("That code doesn't match — please try again or contact the team.")
    st.stop()


# ── Screen 1: Welcome ──────────────────────────────────────────────────────

def screen_welcome():
    _render_logo(height=72)
    st.markdown("## Every child learns in their own way.")
    st.markdown(
        "Genex helps you understand your child's development and gives you "
        "simple, practical activities to do together at home."
    )
    st.markdown("")

    st.markdown(
        "<div class='genex-card'>"
        "<div class='genex-section-label'>What to expect</div>"
        "<p style='margin:0.4rem 0 0'>✦ &nbsp;A few short questions about your child<br>"
        "✦ &nbsp;A personalised activity for today<br>"
        "✦ &nbsp;A gentle weekly plan — only a few minutes a day<br>"
        "✦ &nbsp;Notes to help you talk with your doctor if needed</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown("")
    st.markdown(
        "<div class='genex-card' style='background:#F5F0FF;border:1px solid #DDD6FE'>"
        "<div class='genex-section-label'>Privacy & safety</div>"
        "<p style='margin:0.4rem 0 0;font-size:0.92rem'>"
        "Genex is a support tool, not a medical service. "
        "It does not diagnose, treat, or replace professional advice.<br><br>"
        "Please use your child's <strong>first name only</strong>, age in months, "
        "and no other identifying details. "
        "No photos, documents, or full names.<br><br>"
        "<em>This is a private pilot — please do not share the link.</em>"
        "</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown("")
    if st.button("Let's get started →", type="primary", use_container_width=True):
        go_to("profile")


# ── Screen 2: Child Profile ────────────────────────────────────────────────

def screen_profile():
    progress_bar()
    _render_logo(height=52)
    st.markdown("## Tell us about your child")
    st.caption("This takes about 1 minute. All fields marked * are required.")
    st.markdown("")

    with st.form("profile_form"):

        child_name = st.text_input(
            "Child's first name *",
            placeholder="e.g. Maya",
            help="Please use first name only, not full legal name.",
        )
        st.caption("Please use first name only, not full legal name.")

        st.markdown("**Child's age \\***")
        col_y, col_m = st.columns(2)
        with col_y:
            age_years = st.number_input("Years", min_value=0, max_value=6, value=2, step=1)
        with col_m:
            age_months_part = st.number_input("Months (0–11)", min_value=0, max_value=11, value=0, step=1)
        age_months = int(age_years) * 12 + int(age_months_part)
        if age_months >= 2:
            st.caption(f"= {age_months} months total")

        diagnosis = st.text_input(
            "Does your child have a diagnosis or condition? (optional)",
            placeholder="e.g. none, not sure, speech delay, autism, developmental delay, ADHD concern",
        )

        concern = st.text_area(
            "What is your main concern about your child's development? *",
            placeholder="Describe what you've noticed or what you'd like support with.",
            height=100,
        )

        daily_time = st.number_input(
            "How many minutes a day can you spend on activities with your child? *",
            min_value=5,
            max_value=60,
            value=15,
            step=5,
        )

        submitted = st.form_submit_button(
            "Start the questions →",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        errors = []
        if not child_name.strip():
            errors.append("Please enter your child's first name.")
        if age_months < 2:
            errors.append("Please enter your child's age (must be at least 2 months).")
        if not concern.strip():
            errors.append("Please describe your main concern.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            state = init_state_from_profile(
                name=child_name.strip(),
                chronological_months=int(age_months),
                diagnosis=diagnosis.strip() if diagnosis.strip() else "not specified",
                concern=concern.strip(),
                daily_time_min=int(daily_time),
            )
            ensure_concern_profile(state)
            estimate_all_delays(state)
            st.session_state["genex_state"] = state
            st.session_state.pop("interview_domain_idx", None)
            go_to("interview")


# ── Interview helpers ──────────────────────────────────────────────────────

def _band_score(questions: list, responses: dict) -> float:
    """Average answer score for a band. Pass threshold >= 0.5."""
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
    for k in [f"bq_{category_key}", f"bm_{category_key}",
              f"bi_{category_key}", f"bf_{category_key}"]:
        st.session_state.pop(k, None)


def _advance_domain(state: dict, category_key: str, domain_idx: int):
    _clear_domain_band_state(category_key)
    st.session_state["genex_state"] = state
    st.session_state["interview_domain_idx"] = domain_idx + 1
    st.rerun()


# ── Screen 3: Interview ────────────────────────────────────────────────────

def screen_interview():
    progress_bar()

    state = get_state()
    if not state:
        st.warning("Please start from the beginning.")
        if st.button("← Start Over"):
            go_to("welcome")
        return

    domain_keys = list(DOMAIN_CONFIG.keys())
    domain_idx  = st.session_state.get("interview_domain_idx", 0)

    # All domains done → finalise and generate plan
    if domain_idx >= len(domain_keys):
        # Full-page loading experience — clears previous content
        st.markdown(
            """
            <div style="
                display:flex;flex-direction:column;align-items:center;
                justify-content:center;min-height:65vh;text-align:center;
                padding:2rem
            ">
                <div style="font-size:3.5rem;margin-bottom:1rem">🌱</div>
                <h2 style="font-size:1.8rem;font-weight:700;color:#5B21B6;margin:0 0 0.5rem">
                    Building your personalised plan…
                </h2>
                <p style="color:#6B7280;font-size:1.05rem;margin:0">
                    This takes just a moment.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        for dk in domain_keys:
            finalize_domain_dev_age(state, dk)
        determine_family_guidance_floor(state)
        for dk in domain_keys:
            generate_category_activity_bank(state, dk)
        allocate_weekly_slots(state)
        build_weekly_schedule(state)
        st.session_state["genex_state"] = state
        st.session_state.pop("parent_plan", None)
        st.session_state["plan_just_built"] = True
        save_session_json(state)
        go_to("weekly_plan")
        return

    category_key     = domain_keys[domain_idx]
    parent_label     = DOMAIN_LABELS.get(category_key, DOMAIN_CONFIG[category_key]["display"])
    icon             = DOMAIN_ICONS.get(category_key, "📋")
    child_name       = state["child"]["name"]

    # Initialise band state
    if f"bq_{category_key}" not in st.session_state:
        all_qs = build_milestone_questions(state, category_key)
        bands: dict = {}
        for q in all_qs:
            bands.setdefault(q["months"], []).append(q)
        band_months = sorted(bands.keys())

        if not band_months:
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

    if band_idx >= len(band_months):
        _advance_domain(state, category_key, domain_idx)
        return

    current_month = band_months[band_idx]
    questions     = bands[current_month]

    # ── Header ────────────────────────────────────────────────────────────
    _render_logo(height=48)

    # Overall interview progress bar (1/4 per domain)
    overall_pct = domain_idx / len(domain_keys)
    st.markdown(
        f"<p style='font-size:0.82rem;color:#7C3AED;margin:0 0 0.2rem'>"
        f"Section {domain_idx + 1} of {len(domain_keys)}: {parent_label}</p>",
        unsafe_allow_html=True,
    )
    st.progress(overall_pct)
    st.markdown("")

    st.markdown(f"## {icon} {parent_label}")

    # Microcopy
    st.markdown(
        "<div class='genex-card' style='background:#F5F0FF;padding:0.75rem 1rem;"
        "margin-bottom:0.75rem;border:1px solid #DDD6FE'>"
        "<p style='margin:0;font-size:0.92rem;color:#5B21B6'>"
        "There are no right or wrong answers. "
        "This helps Genex understand what kind of support may be most helpful for your child."
        "</p></div>",
        unsafe_allow_html=True,
    )

    # ── Question form ──────────────────────────────────────────────────────
    cache_key = f"bc_{category_key}_{current_month}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = {}

    with st.form(f"band_form_{category_key}_{current_month}"):
        responses = {}
        answer_keys = list(ANSWER_OPTIONS.keys())

        for q in questions:
            milestone_text = q['milestone'].rstrip('.?')
            st.markdown(
                f"<div class='genex-card' style='margin:0.5rem 0'>"
                f"<p style='font-size:1.08rem;font-weight:600;margin:0'>"
                f"Can {child_name} {milestone_text}?</p>"
                f"</div>",
                unsafe_allow_html=True,
            )
            sel = st.radio(
                label=f"answer_{q['question_id']}",
                options=answer_keys,
                index=st.session_state[cache_key].get(q["question_id"], None),
                key=f"bq_{q['question_id']}",
                horizontal=True,
                label_visibility="collapsed",
            )
            responses[q["question_id"]] = sel
            st.markdown("")

        submitted = st.form_submit_button(
            "Next →",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        # Validate all questions answered
        unanswered = [qid for qid, val in responses.items() if val is None]
        if unanswered:
            st.error("Please answer all questions before continuing.")
            st.stop()

        # Cache selections for back navigation
        for qid, sel in responses.items():
            st.session_state[cache_key][qid] = answer_keys.index(sel)

        # Record answers
        for q in questions:
            record_answer(state, category_key, q,
                          ANSWER_OPTIONS[responses[q["question_id"]]])
        st.session_state["genex_state"] = state

        # Adaptive stopping: 2 consecutive fails → advance domain
        score  = _band_score(questions, responses)
        passed = score >= 0.5

        if passed:
            st.session_state[f"bf_{category_key}"] = 0
        else:
            st.session_state[f"bf_{category_key}"] = consec_fails + 1

        updated_fails = st.session_state[f"bf_{category_key}"]
        next_idx      = band_idx + 1

        if updated_fails >= 2 or next_idx >= len(band_months):
            _advance_domain(state, category_key, domain_idx)
        else:
            st.session_state[f"bi_{category_key}"] = next_idx
            st.rerun()

    # ── Back navigation ────────────────────────────────────────────────────
    st.markdown("")
    col_back, _ = st.columns([1, 3])
    with col_back:
        if band_idx > 0:
            if st.button(f"← Back", key="back_band"):
                prev_month = band_months[band_idx - 1]
                n = len(bands[prev_month])
                if state["qna"].get(category_key):
                    state["qna"][category_key] = state["qna"][category_key][:-n]
                st.session_state["genex_state"] = state
                st.session_state[f"bi_{category_key}"] = band_idx - 1
                st.session_state[f"bf_{category_key}"] = 0
                st.rerun()
        elif domain_idx > 0:
            if st.button("← Back", key="back_domain"):
                prev_key = domain_keys[domain_idx - 1]
                _clear_domain_band_state(category_key)
                state["qna"][category_key] = []
                _clear_domain_band_state(prev_key)
                state["qna"][prev_key] = []
                st.session_state["genex_state"] = state
                st.session_state["interview_domain_idx"] = domain_idx - 1
                st.rerun()


# ── Screen 4: Today's Activity (hero) ─────────────────────────────────────

def _get_today_activities(state: dict) -> list:
    """Return all activities scheduled for today (Monday first weekday with activities)."""
    schedule = state.get("weekly_schedule", {})
    days = schedule.get("days", {})
    for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
        items = days.get(day, {}).get("items", [])
        if items:
            return items
    return []


def _build_why_helps(activity: dict, state: dict) -> str:
    """Generate a parent-friendly 'why this helps' sentence."""
    category_key = activity.get("category_key", "")
    WHY = {
        "movement_and_physical": (
            "Physical play builds strength, coordination, and body confidence — "
            "skills that support everything from dressing independently to playing with friends."
        ),
        "language_and_communication": (
            "Practising communication, even in small moments, builds the connection between "
            "hearing, understanding, and expressing — the foundation of language."
        ),
        "social_and_emotional": (
            "Small social moments — like taking turns or making eye contact — teach your child "
            "how to connect, trust, and feel safe in the world."
        ),
        "cognitive": (
            "Play that involves thinking, problem-solving, and exploration helps your child "
            "build attention, memory, and the ability to learn new things."
        ),
    }
    return WHY.get(category_key, "This activity supports your child's development in a meaningful way.")



def _render_activity_detail(item: dict, child_name: str, key_prefix: str = ""):
    """Render full activity detail inside an expander or section."""
    icon      = DOMAIN_ICONS.get(item.get("category_key", ""), "🌱")
    cat_label = DOMAIN_LABELS.get(item.get("category_key", ""), item.get("category", ""))
    why       = _build_why_helps(item, {})

    st.markdown(
        f"<p style='font-size:0.88rem;color:#6B7280;margin:0 0 0.6rem'>"
        f"{icon} {cat_label} &nbsp;·&nbsp; ⏱ {item.get('duration_min', 5)} min</p>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='genex-section'>"
        f"<div class='genex-section-label'>⭐ Why this helps</div>"
        f"<p style='margin:0'>{why}</p>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='genex-section'>"
        f"<div class='genex-section-label'>👣 How to do it</div>"
        f"<p style='margin:0'>{item.get('instructions', '')}</p>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if item.get("materials"):
        st.markdown(
            f"<div class='genex-section'>"
            f"<div class='genex-section-label'>🧰 What you need</div>"
            f"<p style='margin:0'>{item.get('materials')}</p>"
            f"</div>",
            unsafe_allow_html=True,
        )
    if item.get("group_note"):
        st.markdown(
            f"<div class='genex-section' style='background:#F0FDF4;border:1px solid #BBF7D0'>"
            f"<div class='genex-section-label'>👧👦 With other kids around</div>"
            f"<p style='margin:0'>{item.get('group_note')}</p>"
            f"</div>",
            unsafe_allow_html=True,
        )


def screen_today():
    progress_bar()

    state = get_state()
    if not state.get("weekly_schedule"):
        st.warning("No plan yet — please complete the questions first.")
        if st.button("← Start Over"):
            go_to("welcome")
        return

    child_name  = state["child"]["name"]
    activities  = _get_today_activities(state)

    _render_logo(height=52)
    st.markdown(f"## Your plan is ready, {child_name}'s parent! 🎉")
    st.markdown(
        "Here are today's activities. They're short and playful — "
        "even one a day makes a real difference."
    )
    st.markdown("")

    if not activities:
        st.info("We couldn't generate specific activities. Please check the weekly plan.")
    else:
        for i, activity in enumerate(activities):
            icon      = DOMAIN_ICONS.get(activity.get("category_key", ""), "🌱")
            cat_label = DOMAIN_LABELS.get(activity.get("category_key", ""),
                                          activity.get("category", ""))
            is_hero   = (i == 0)

            if is_hero:
                # First activity gets the full hero treatment
                st.markdown(
                    f"<div class='genex-hero-card'>"
                    f"<div class='genex-section-label'>Today's focus — {icon} {cat_label}</div>"
                    f"<div class='genex-hero-title'>{activity.get('title', '')}</div>"
                    f"<p style='color:#6B7280;font-size:0.9rem;margin:0'>"
                    f"⏱ {activity.get('duration_min', 5)} min &nbsp;·&nbsp; "
                    f"🧰 {activity.get('materials', 'common household items')}</p>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                _render_activity_detail(activity, child_name, key_prefix=f"today_{i}")
            else:
                # Additional activities as secondary cards with expander
                st.markdown("")
                with st.expander(
                    f"{icon} Also today: **{activity.get('title', '')}** "
                    f"— {activity.get('duration_min', 5)} min",
                    expanded=False,
                ):
                    _render_activity_detail(activity, child_name, key_prefix=f"today_{i}")

            st.markdown("")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("📅 See Weekly Plan", use_container_width=True, type="primary"):
            go_to("weekly_plan")
    with col2:
        if st.button("🩺 Doctor Notes", use_container_width=True):
            go_to("doctor_prep")


# ── Screen 5: Weekly Plan ──────────────────────────────────────────────────

def _get_activity_bank_flat(state: dict) -> list:
    """Return all activities from all banks as a flat list."""
    result = []
    for category_key in DOMAIN_CONFIG:
        bank = state.get("activity_banks", {}).get(category_key, {})
        for a in bank.get("activities", []):
            result.append({**a, "category_key": category_key})
    return result


def screen_weekly_plan():
    progress_bar()

    state = get_state()
    if not state.get("weekly_schedule"):
        st.warning("No plan yet. Please complete the questions first.")
        if st.button("← Back"):
            go_to("interview")
        return

    child_name = state["child"]["name"]
    schedule   = state["weekly_schedule"]

    # Initialise editable plan once per session
    if "parent_plan" not in st.session_state:
        st.session_state["parent_plan"] = copy.deepcopy(schedule.get("days", {}))

    plan: dict = st.session_state["parent_plan"]

    _render_logo(height=52)

    # Show "plan ready" banner only on first arrival after generation
    if st.session_state.pop("plan_just_built", False):
        st.markdown(
            f"<div class='genex-hero-card' style='text-align:center;padding:1.5rem'>"
            f"<div style='font-size:2.2rem;margin-bottom:0.4rem'>🎉</div>"
            f"<div class='genex-hero-title' style='font-size:1.5rem'>"
            f"{child_name}'s plan is ready!</div>"
            f"<p style='color:#6B7280;margin:0.3rem 0 0'>Tap any activity to see full details, "
            f"remove ones that don't fit, or add new ones below.</p>"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.markdown("")

    st.markdown(f"## 📅 {child_name}'s Weekly Plan")
    st.caption(
        "Tap any activity to see details. "
        "Remove activities you don't like — or add new ones on the weekend."
    )
    st.markdown("")

    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    WEEKEND  = ["Saturday", "Sunday"]
    ALL_DAYS = WEEKDAYS + WEEKEND

    activity_bank = _get_activity_bank_flat(state)

    for day in ALL_DAYS:
        day_info = plan.get(day, {"items": [], "total_minutes": 0, "is_weekend": day in WEEKEND})
        items    = day_info.get("items", [])
        is_wknd  = day_info.get("is_weekend", day in WEEKEND)
        total    = sum(int(it.get("duration_min", 0)) for it in items)

        wknd_style = "color:#92400E;font-weight:700" if is_wknd else "color:#4A6080;font-weight:700"
        st.markdown(
            f"<div class='genex-day-header' style='{wknd_style}'>"
            f"{'🌤 ' if is_wknd else ''}{day}"
            + (f"  ·  {total} min" if total else "")
            + "</div>",
            unsafe_allow_html=True,
        )

        if not items:
            st.markdown(
                "<div style='color:#9CA3AF;font-style:italic;font-size:0.9rem;"
                "padding:0.4rem 0 0.8rem'>Rest day — nothing scheduled.</div>",
                unsafe_allow_html=True,
            )
        else:
            for idx in range(len(items)):
                item = plan[day]["items"][idx]
                icon      = DOMAIN_ICONS.get(item.get("category_key", ""), "🌱")
                cat_label = DOMAIN_LABELS.get(item.get("category_key", ""),
                                              item.get("category", ""))

                col_exp, col_del = st.columns([11, 1])
                with col_exp:
                    with st.expander(
                        f"{icon} **{item.get('title', '')}** — {item.get('duration_min', 5)} min",
                        expanded=False,
                    ):
                        _render_activity_detail(item, child_name,
                                                key_prefix=f"{day}_{idx}")
                with col_del:
                    if st.button("✕", key=f"remove_{day}_{idx}",
                                 help="Remove this activity"):
                        plan[day]["items"].pop(idx)
                        st.rerun()

        # Add from activity bank — available on every day
        bank_options = {
            f"{DOMAIN_ICONS.get(a.get('category_key',''), '🌱')} "
            f"{a.get('title','')} ({a.get('duration_min',5)} min)": a
            for a in activity_bank
            if a.get("title") not in [i.get("title") for i in plan.get(day, {}).get("items", [])]
        }
        if bank_options:
            with st.expander(f"➕ Add an activity to {day}", expanded=False):
                selected_label = st.selectbox(
                    "Choose from your activity bank:",
                    options=list(bank_options.keys()),
                    key=f"add_select_{day}",
                    label_visibility="collapsed",
                )
                selected_activity = bank_options[selected_label]
                # Preview
                st.markdown(
                    f"<div class='genex-section' style='margin:0.4rem 0'>"
                    f"<p style='font-size:0.93rem;margin:0'>"
                    f"{selected_activity.get('instructions','')[:180]}…</p>"
                    f"<p style='font-size:0.82rem;color:#9CA3AF;margin:0.3rem 0 0'>"
                    f"🧰 {selected_activity.get('materials','')}</p>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                if st.button(f"Add to {day}", key=f"add_btn_{day}", type="primary"):
                    if day not in plan:
                        plan[day] = {"items": [], "total_minutes": 0, "is_weekend": is_wknd}
                    plan[day]["items"].append(selected_activity)
                    st.rerun()

        st.markdown("")

    # Download
    plan_text = _build_plan_text_from(plan, child_name)
    st.download_button(
        label="⬇️ Download Weekly Plan",
        data=plan_text,
        file_name=f"genex_plan_{child_name}_{datetime.now().strftime('%Y%m%d')}.txt",
        mime="text/plain",
        use_container_width=True,
    )

    st.markdown("")
    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("← Back", use_container_width=True):
            go_to("interview")
    with col2:
        if st.button("🩺 Doctor Notes →", type="primary", use_container_width=True):
            go_to("doctor_prep")
    with col3:
        restart_button()


def _build_plan_text_from(plan: dict, child_name: str) -> str:
    """Build a plain-text version of the editable weekly plan for download."""
    lines = [
        "Genex Parent Copilot — Weekly Plan",
        f"Child: {child_name}",
        f"Generated: {datetime.now().strftime('%B %d, %Y')}",
        "=" * 50,
        "",
    ]
    for day in ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]:
        items = plan.get(day, {}).get("items", [])
        lines.append(f"\n{day.upper()}")
        if not items:
            lines.append("  Rest day")
        else:
            for item in items:
                lines.append(f"\n  {item.get('title','')}")
                lines.append(f"  Duration: {item.get('duration_min',5)} min")
                lines.append(f"  Materials: {item.get('materials','')}")
                lines.append(f"  {item.get('instructions','')}")
    lines += [
        "",
        "=" * 50,
        "This plan is for home support only.",
        "It does not replace professional advice.",
    ]
    return "\n".join(lines)


# ── Screen 6: Doctor-Prep Notes ────────────────────────────────────────────

def screen_doctor_prep():
    progress_bar()

    state = get_state()
    if not state.get("dev_age"):
        st.warning("Please complete the questions first.")
        return

    _render_logo(height=52)
    st.markdown("## 🩺 Notes for Your Doctor")
    st.caption(
        "These are just conversation starters — things you might want to bring up "
        "at your child's next appointment. Genex does not make diagnoses."
    )
    st.markdown("")

    prep       = build_doctor_visit_prep(state)
    child_name = state["child"]["name"]

    # Domains with something to discuss
    if prep.get("priority_domains"):
        st.markdown(
            "<div class='genex-doctor-card'>"
            "<div class='genex-section-label' style='color:#4A6080'>Areas worth discussing</div>"
            "<p style='margin:0.4rem 0 0;font-size:0.95rem'>",
            unsafe_allow_html=True,
        )
        for r in prep["priority_domains"]:
            parent_label = DOMAIN_LABELS.get(
                next((k for k,v in DOMAIN_CONFIG.items() if v["display"]==r["category"]), ""),
                r["category"]
            )
            st.markdown(
                f"<div class='genex-section' style='margin:0.5rem 0'>"
                f"<p style='font-weight:600;margin:0 0 0.3rem'>{parent_label}</p>"
                f"<p style='margin:0;font-size:0.93rem'>"
                f"Based on your answers, this is an area that may be worth mentioning to your doctor."
                f"</p></div>",
                unsafe_allow_html=True,
            )
        st.markdown("</p></div>", unsafe_allow_html=True)

    if prep.get("monitor_domains"):
        st.markdown("")
        st.markdown(
            "<div class='genex-section-label' style='color:#4A6080'>Worth keeping an eye on</div>",
            unsafe_allow_html=True,
        )
        for r in prep["monitor_domains"]:
            parent_label = DOMAIN_LABELS.get(
                next((k for k,v in DOMAIN_CONFIG.items() if v["display"]==r["category"]), ""),
                r["category"]
            )
            st.markdown(f"- **{parent_label}**")

    # Questions to ask
    st.markdown("")
    st.markdown(
        "<div class='genex-card'>"
        "<div class='genex-section-label'>Questions to ask your pediatrician or specialist</div>",
        unsafe_allow_html=True,
    )
    for q in prep.get("questions_for_doctor", []):
        # Personalise the question with child's name
        q_personal = q.replace("the child", child_name).replace("your child", child_name)
        st.markdown(
            f"<p style='margin:0.4rem 0;font-size:0.95rem'>❓ {q_personal}</p>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    # Disclaimer
    st.markdown("")
    st.info(
        "**Remember:** Genex is a support tool, not a medical service. "
        "These notes are meant to support a conversation with your doctor, "
        "not replace their assessment."
    )

    st.markdown("")
    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("← Weekly Plan", use_container_width=True):
            go_to("weekly_plan")
    with col2:
        if st.button("Share Feedback →", type="primary", use_container_width=True):
            go_to("feedback")
    with col3:
        restart_button()


# ── Screen 7: Feedback ─────────────────────────────────────────────────────

def screen_feedback():
    progress_bar()

    _render_logo(height=52)
    st.markdown("## 💬 Share Your Feedback")
    st.markdown(
        "You're part of a small pilot that will shape how Genex develops. "
        "Your honest feedback — positive or critical — matters a lot."
    )
    st.markdown("")

    state = get_state()
    child = state.get("child", {})

    with st.form("feedback_form"):

        overall = st.radio(
            "Overall, how useful did you find Genex?",
            options=["Very useful", "Somewhat useful", "Not very useful", "Not useful at all"],
            horizontal=True,
        )

        activity_rating = st.radio(
            "How relevant did Today's Activity feel for your child?",
            options=["Very relevant", "Somewhat relevant", "Not very relevant", "Not sure"],
            horizontal=True,
        )

        language_rating = st.radio(
            "Was the language clear and easy to understand?",
            options=["Yes, very clear", "Mostly clear", "Sometimes confusing", "Hard to understand"],
            horizontal=True,
        )

        st.markdown("")
        what_helped = st.text_area(
            "What was most helpful?",
            height=80,
            placeholder="Anything you found useful or reassuring…",
        )
        what_change = st.text_area(
            "What would you change or improve?",
            height=80,
            placeholder="Anything confusing, missing, or that didn't feel right…",
        )
        general = st.text_area(
            "Anything else you'd like to share?",
            height=80,
            placeholder="Open comments…",
        )

        submitted = st.form_submit_button(
            "Submit Feedback",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            feedback = {
                "child_name":     child.get("name", "unknown"),
                "age_months":     child.get("chronological_months"),
                "diagnosis":      child.get("diagnosis"),
                "ratings": {
                    "overall_usefulness":     overall,
                    "activity_relevance":     activity_rating,
                    "language_clarity":       language_rating,
                },
                "comments": {
                    "what_helped":   what_helped,
                    "what_to_change": what_change,
                    "general":       general,
                },
                "submitted_at": stamp,
                "app_version":  "parent-copilot-v0.2",
            }
            fname  = f"feedback_{child.get('name','parent')}_{stamp}.json"
            result = _storage_save(feedback, f"feedback/{fname}", SESSION_DIR)
            if result in ("gcs", "local"):
                st.success(
                    "✅ Thank you — your feedback has been saved. "
                    "It will directly shape the next version of Genex."
                )
            else:
                st.warning("Feedback could not be saved automatically. Please screenshot this page.")
        except Exception as e:
            st.error(f"Could not save feedback: {e}")

    st.markdown("")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Doctor Notes", use_container_width=True):
            go_to("doctor_prep")
    with col2:
        restart_button()


# ── Sidebar ────────────────────────────────────────────────────────────────

def sidebar_nav():
    with st.sidebar:
        _render_logo(height=52)
        st.markdown("### Genex Parent Copilot")
        st.caption("Navigation")
        for s in SCREENS:
            label = SCREEN_LABELS[s]
            if st.button(label, key=f"nav_{s}", use_container_width=True):
                go_to(s)
        state = get_state()
        if state.get("child"):
            child = state["child"]
            st.divider()
            st.caption(
                f"**{child.get('name', '—')}** · {child.get('chronological_months', '—')} months"
            )


# ── Router ─────────────────────────────────────────────────────────────────

def main():
    if "screen" not in st.session_state:
        st.session_state["screen"] = "welcome"

    _password_gate()
    sidebar_nav()

    screen = current_screen()

    if screen == "welcome":
        screen_welcome()
    elif screen == "profile":
        screen_profile()
    elif screen == "interview":
        screen_interview()
    elif screen == "weekly_plan":
        screen_weekly_plan()
    elif screen == "doctor_prep":
        screen_doctor_prep()
    elif screen == "feedback":
        screen_feedback()
    else:
        go_to("welcome")


if __name__ == "__main__":
    main()
