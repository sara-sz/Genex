"""
app.py — Genex Parent Copilot v0.3-auth-staging
------------------------------------------------
Privacy-first, per-account parent developmental support app.

Changes from v0.2
-----------------
- Per-account email/password auth via Identity Platform (mock mode for local dev)
- Allowlist-gated registration (open registration disabled)
- Child first name stored only in session state — never persisted to GCS
- GCS paths use user_id, not child name: sessions/{user_id}/{session_id}.json
- Child name stripped before genex_core / OpenAI calls ("your child")
- Session JSON uses new privacy schema (no name, adds user_id, session_id,
  child_id, consent_given, consent_timestamp, app_version, engine_version)
- Privacy notice + consent checkbox at registration
- Persistent footer: "not a diagnostic tool" disclaimer + privacy notice link
- Shared password gate removed

Auth modes (AUTH_MODE env var):
  mock               — local dev, in-memory users, no cloud setup needed
  identity_platform  — Google Cloud Identity Platform / Firebase Auth

Run: streamlit run app.py
"""

import copy
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# ── Page config (must be first Streamlit call) ─────────────────────────────
st.set_page_config(
    page_title="Genex — Parent Copilot",
    page_icon="🌱",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ── Auth + allowlist modules ───────────────────────────────────────────────
import auth
import allowlist as al

# ── Load CSS (cached) ──────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def _load_css() -> str:
    p = Path(__file__).parent / "assets" / "style.css"
    return p.read_text() if p.exists() else ""

st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)

# ── genex_core imports ─────────────────────────────────────────────────────
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

# Pre-warm CDC milestone table once at startup
@st.cache_resource(show_spinner=False)
def _prewarm_milestones():
    from genex_core.milestones import get_cdc_df
    get_cdc_df()

_prewarm_milestones()

# ── Constants ──────────────────────────────────────────────────────────────
APP_VERSION    = "parent-copilot-v0.3-auth-staging"
ENGINE_VERSION = "v11"

# Screens visible after login
MAIN_SCREENS = ["welcome", "profile", "interview", "weekly_plan", "doctor_prep", "feedback"]

SCREEN_LABELS = {
    "welcome":      "Welcome",
    "profile":      "Your Child",
    "interview":    "Quick Questions",
    "weekly_plan":  "Weekly Plan",
    "doctor_prep":  "Doctor Notes",
    "feedback":     "Share Feedback",
}

ANSWER_OPTIONS = {
    "Yes, usually":    "yes",
    "Sometimes":       "sometimes",
    "Only with help":  "with_help",
    "Not yet":         "no",
    "Not sure":        "not_sure",
}

DOMAIN_LABELS = {
    "movement_and_physical":       "Moving and Playing",
    "language_and_communication":  "Talking and Communicating",
    "social_and_emotional":        "Connecting and Feeling",
    "cognitive":                   "Learning and Exploring",
}

DOMAIN_ICONS = {
    "movement_and_physical":       "🏃",
    "language_and_communication":  "💬",
    "social_and_emotional":        "💛",
    "cognitive":                   "🧩",
}

SESSION_DIR  = Path(os.environ.get("SESSION_DIR", "outputs/sessions"))
FEEDBACK_DIR = SESSION_DIR.parent / "feedback"   # outputs/feedback (or /tmp/feedback on Cloud Run)
SESSION_DIR.mkdir(parents=True, exist_ok=True)
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


# ── Display name helpers ───────────────────────────────────────────────────

def get_child_display_name() -> str:
    """
    Return the child's first name for UI display only.
    Falls back to "your child" — never reads from stored state to avoid
    accidentally using a name that was passed to the engine.
    """
    return st.session_state.get("child_display_name", "your child")


# ── Logo ───────────────────────────────────────────────────────────────────
import base64

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


# ── Navigation helpers ─────────────────────────────────────────────────────

def go_to(screen: str):
    st.session_state["screen"] = screen
    st.rerun()


def current_screen() -> str:
    return st.session_state.get("screen", "login")


def get_state() -> dict:
    if "genex_state" not in st.session_state:
        st.session_state["genex_state"] = {}
    return st.session_state["genex_state"]


def progress_bar():
    screen = current_screen()
    if screen not in MAIN_SCREENS or screen == "welcome":
        return
    screens_with_progress = [s for s in MAIN_SCREENS if s != "welcome"]
    try:
        idx = screens_with_progress.index(screen)
    except ValueError:
        idx = 0
    pct   = int(((idx + 1) / len(screens_with_progress)) * 100)
    label = SCREEN_LABELS.get(screen, "")
    st.progress(pct, text=f"**{label}** — Step {idx + 1} of {len(screens_with_progress)}")
    st.markdown("")


def restart_button():
    if st.button("🔄 Start Over", use_container_width=True):
        # Keep auth, clear session/plan
        for key in [
            "genex_state", "parent_plan", "interview_domain_idx",
            "child_display_name", "child_concern", "plan_just_built",
            "session_id", "child_id",
        ]:
            st.session_state.pop(key, None)
        for key in list(st.session_state.keys()):
            if key.startswith(("bq_", "bm_", "bi_", "bf_", "bc_")):
                del st.session_state[key]
        go_to("welcome")


# ── Session storage ────────────────────────────────────────────────────────

# Internal tier keys → parent-safe labels (mirrors activity_engine._tier_to_display)
_TIER_DISPLAY = {
    "needs_special_support": "Extra Support",
    "monitor_and_enrich":    "Monitor & Enrich",
    "enrich_and_observe":    "Monitor & Enrich",
    "no_special_support":    "On Track",
}


def _sanitize_plan_for_storage(weekly_schedule: dict) -> dict:
    """
    Replace internal tier keys in activity goal fields with parent-safe labels.
    Operates on a deep copy so the live UI state is never mutated.
    """
    import copy
    plan = copy.deepcopy(weekly_schedule)
    days = plan.get("days", {})
    for day_data in days.values():
        for item in day_data.get("items", []):
            raw_goal = item.get("goal", "")
            item["goal"] = _TIER_DISPLAY.get(raw_goal, raw_goal)
    return plan


def _save_consent_record(user_id: str, email: str, timestamp: str):
    """
    Persist consent record to consent/{user_id}.json in GCS (or local fallback).
    Called once at registration. Never raises.
    """
    try:
        record = {
            "user_id":           user_id,
            "email":             email,
            "consent_given":     True,
            "consent_timestamp": timestamp,
            "app_version":       APP_VERSION,
        }
        blob_name = f"consent/{user_id}.json"
        consent_dir = SESSION_DIR.parent / "consent"
        _storage_save(record, blob_name, consent_dir)
    except Exception as exc:
        print(f"[app] _save_consent_record failed: {exc}")


def _load_consent_record(user_id: str) -> tuple:
    """
    Load consent record for a returning user from GCS (or local fallback).
    Returns (consent_given: bool, consent_timestamp: str).
    Falls back to (False, "") if not found.
    """
    from genex_core.storage import GCS_BUCKET_NAME
    try:
        if GCS_BUCKET_NAME:
            from google.cloud import storage as gcs
            client = gcs.Client()
            blob = client.bucket(GCS_BUCKET_NAME).blob(f"consent/{user_id}.json")
            if blob.exists():
                import json as _json
                data = _json.loads(blob.download_as_text())
                return data.get("consent_given", False), data.get("consent_timestamp", "")
        else:
            consent_path = SESSION_DIR.parent / "consent" / f"{user_id}.json"
            if consent_path.exists():
                import json as _json
                data = _json.loads(consent_path.read_text(encoding="utf-8"))
                return data.get("consent_given", False), data.get("consent_timestamp", "")
    except Exception as exc:
        print(f"[app] _load_consent_record failed: {exc}")
    return False, ""


def save_session_json(state: dict):
    """
    Persist a de-identified session snapshot to GCS (or local fallback).
    - No child name stored anywhere in the JSON or file path.
    - Stored under sessions/{user_id}/{session_id}.json
    - Internal tier labels remapped to parent-safe labels before saving.
    - Never raises.
    """
    try:
        user       = auth.get_current_user()
        user_id    = user["uid"] if user else "anonymous"
        session_id = st.session_state.setdefault("session_id", str(uuid.uuid4()))
        child_id   = st.session_state.setdefault("child_id",   str(uuid.uuid4()))

        child = state.get("child", {})
        snapshot = {
            "user_id":                user_id,
            "session_id":             session_id,
            "child_id":               child_id,
            "age_months":             child.get("chronological_months"),
            "diagnosis_or_condition": child.get("diagnosis"),
            "concern":                st.session_state.get("child_concern", ""),
            "answers":                state.get("qna", {}),
            "generated_plan":         _sanitize_plan_for_storage(
                                          state.get("weekly_schedule", {})),
            "feedback":               None,
            "created_at":             datetime.now(timezone.utc).isoformat(),
            "app_version":            APP_VERSION,
            "engine_version":         ENGINE_VERSION,
            "consent_given":          st.session_state.get("consent_given", False),
            "consent_timestamp":      st.session_state.get("consent_timestamp", ""),
        }
        blob_name = f"sessions/{user_id}/{session_id}.json"
        _storage_save(snapshot, blob_name, SESSION_DIR / user_id)
    except Exception as exc:
        print(f"[app] save_session_json failed: {exc}")


# ── Auth header (shown on every authenticated screen) ─────────────────────

def _render_auth_header():
    """Small top bar showing signed-in email and a sign-out button."""
    user = auth.get_current_user()
    if not user:
        return
    col_email, col_btn = st.columns([5, 1])
    with col_email:
        st.markdown(
            f"<p style='font-size:0.8rem;color:#9CA3AF;margin:0;padding-top:0.3rem'>"
            f"Signed in as <strong>{user['email']}</strong></p>",
            unsafe_allow_html=True,
        )
    with col_btn:
        if st.button("Sign out", key="top_signout"):
            auth.sign_out()
            st.rerun()
    st.markdown(
        "<hr style='margin:0.4rem 0 1rem;border-top:1px solid #EDE9FE'>",
        unsafe_allow_html=True,
    )


# ── Footer ─────────────────────────────────────────────────────────────────

def _render_footer():
    st.markdown(
        "<div style='text-align:center;margin-top:2.5rem;padding-top:1rem;"
        "border-top:1px solid #EDE9FE'>"
        "<p style='font-size:0.8rem;color:#9CA3AF;margin:0'>"
        "Genex is not a diagnostic tool and does not provide medical advice. "
        "Always consult a qualified healthcare professional for your child's development."
        "</p></div>",
        unsafe_allow_html=True,
    )
    # Privacy notice link as a low-profile button
    col = st.columns([1, 2, 1])[1]
    with col:
        if st.button("Privacy Notice", key="footer_privacy", use_container_width=True):
            st.session_state["_return_screen"] = current_screen()
            go_to("privacy_policy")


# ── AUTH SCREENS ───────────────────────────────────────────────────────────

def screen_login():
    _render_logo(height=64)
    st.markdown("## Welcome to Genex")
    st.markdown("Sign in to access your child's plan.")
    st.markdown("")

    if auth.AUTH_MODE == "mock":
        st.info(
            "**Local dev mode** (AUTH_MODE=mock) — "
            "register with any allowlisted email and a 6+ character password.",
            icon="🔧",
        )

    with st.form("login_form"):
        email    = st.text_input("Email address", placeholder="you@example.com")
        password = st.text_input("Password", type="password", placeholder="Your password")
        submitted = st.form_submit_button(
            "Sign in →", type="primary", use_container_width=True
        )

    if submitted:
        if not email.strip() or not password:
            st.error("Please enter your email and password.")
        else:
            ok, err, uid, token = auth.login(email, password)
            if ok:
                st.session_state["auth_user"] = {
                    "uid":      uid,
                    "email":    email.strip().lower(),
                    "id_token": token,
                }
                consent_given, consent_ts = _load_consent_record(uid)
                st.session_state["consent_given"]     = consent_given
                st.session_state["consent_timestamp"] = consent_ts
                go_to("welcome")
            else:
                st.error(err)

    st.markdown("")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Create an account", use_container_width=True):
            go_to("register")
    with col2:
        if st.button("Forgot password?", use_container_width=True):
            go_to("reset_password")

    _render_footer()


def screen_register():
    _render_logo(height=64)
    st.markdown("## Create your Genex account")
    st.markdown(
        "Genex is currently in private beta. "
        "Only email addresses on our beta list can register."
    )
    st.markdown("")

    if auth.AUTH_MODE == "mock":
        st.info(
            "**Local dev mode** — edit `config/allowlist.json` to add test emails.",
            icon="🔧",
        )

    with st.form("register_form"):
        email    = st.text_input("Email address", placeholder="you@example.com")
        password = st.text_input(
            "Create a password",
            type="password",
            placeholder="At least 6 characters",
        )
        confirm  = st.text_input(
            "Confirm password",
            type="password",
            placeholder="Repeat your password",
        )

        st.markdown("")
        st.markdown(
            "<div class='genex-card' style='background:#F5F0FF;border:1px solid #DDD6FE;"
            "font-size:0.88rem;padding:0.85rem 1rem'>"
            "<strong>Before you register, please read:</strong><br><br>"
            "• Genex collects your child's age, developmental concern, and milestone answers "
            "to generate a personalised activity plan.<br>"
            "• We use your child's first name only to personalise the on-screen experience. "
            "We do not store it in files or send it to AI services.<br>"
            "• We do <strong>not</strong> collect full name, date of birth, "
            "school, doctor's name, photos, or documents.<br>"
            "• The Genex team may review de-identified pilot data to improve the product, "
            "safety, and quality. We do not sell your data.<br>"
            "• Genex is <strong>not a diagnostic tool</strong> and does not replace "
            "professional medical or developmental evaluation.<br><br>"
            "<a href='#' style='color:#7C3AED'>Read full Privacy Notice →</a>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.markdown("")

        consent = st.checkbox(
            "I have read and agree to the Genex Privacy Notice. "
            "I understand Genex is not a diagnostic tool.",
        )

        submitted = st.form_submit_button(
            "Create account →", type="primary", use_container_width=True
        )

    if submitted:
        errors = []
        if not email.strip():
            errors.append("Please enter your email address.")
        if not password:
            errors.append("Please create a password.")
        if password != confirm:
            errors.append("Passwords don't match.")
        if not consent:
            errors.append("Please read and accept the Privacy Notice to continue.")

        if errors:
            for e in errors:
                st.error(e)
        elif not al.is_allowed(email):
            st.error(
                "This email isn't on our beta list. "
                "Sign up at genex.dev to join the waitlist."
            )
        else:
            ok, err, uid = auth.register(email, password)
            if ok:
                now = datetime.now(timezone.utc).isoformat()
                st.session_state["auth_user"] = {
                    "uid":      uid,
                    "email":    email.strip().lower(),
                    "id_token": "",
                }
                st.session_state["consent_given"]     = True
                st.session_state["consent_timestamp"] = now
                _save_consent_record(uid, email.strip().lower(), now)
                go_to("welcome")
            else:
                st.error(err)

    st.markdown("")
    if st.button("← Already have an account? Sign in", use_container_width=True):
        go_to("login")

    _render_footer()


def screen_reset_password():
    _render_logo(height=56)
    st.markdown("## Reset your password")
    st.markdown(
        "Enter the email you registered with and we'll send you a reset link."
    )
    st.markdown("")

    with st.form("reset_form"):
        email     = st.text_input("Email address", placeholder="you@example.com")
        submitted = st.form_submit_button(
            "Send reset link →", type="primary", use_container_width=True
        )

    if submitted:
        if not email.strip():
            st.error("Please enter your email address.")
        else:
            ok, msg = auth.send_password_reset(email)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    st.markdown("")
    if st.button("← Back to sign in", use_container_width=True):
        go_to("login")

    _render_footer()


def screen_privacy_policy():
    _render_logo(height=48)
    st.markdown("## Genex Privacy Notice")
    st.caption(f"Version: {APP_VERSION} · Last updated: May 2026")
    st.markdown("")

    st.markdown("""
**What we collect**

When you use Genex, we collect:
- Your email address (for account access)
- Your child's age in months
- Your child's primary diagnosis or developmental concern (free text)
- Your answers to developmental milestone questions
- The activity plan we generate for your child
- Any feedback you choose to submit

**What we do not collect**

- Your child's full name or date of birth
- Your child's school, doctor, or therapist name
- Your address or phone number
- Photos, videos, documents, or voice recordings
- Payment or insurance information

**How we use your data**

- To generate your child's personalised activity plan
- To save your plan so you can return to it
- The Genex team may review de-identified or limited pilot data to improve the
  product, evaluate quality, and ensure safety. We do not use your data for
  advertising.
- We do not sell your data to any third party.

**Where your data is stored**

Data is stored on Google Cloud (us-central1). Genex has reviewed and accepted
Google Cloud's data processing terms. We use your child's first name only to
personalize the on-screen experience during the session. We do not store it in
session files, feedback files, or send it to AI services.

**AI-generated content**

Activity suggestions may be generated with the help of an AI service
(OpenAI). We send only your child's age, developmental domain, and concern
summary — never their name or identifying details.

**Your rights**

You can request deletion of your data at any time by emailing:
**info@getgenex.com**. We will delete your account and all associated session
data within 7 days.

**Not a medical tool**

Genex is not a diagnostic tool and does not provide medical advice.
The activity plans and notes Genex generates are for informational and
home-support purposes only. They do not replace assessment or treatment by a
qualified healthcare or educational professional. Always consult your child's
doctor, therapist, or specialist for medical or developmental decisions.

**Contact**

Questions about this notice: info@getgenex.com
""")

    st.markdown("")
    return_screen = st.session_state.pop("_return_screen", None)
    label = "← Back" if return_screen else "← Sign in"
    if st.button(label, use_container_width=True):
        go_to(return_screen if return_screen else "login")


# ── MAIN APP SCREENS ───────────────────────────────────────────────────────

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
        "✦ &nbsp;A personalised weekly activity plan<br>"
        "✦ &nbsp;A gentle routine — only a few minutes a day<br>"
        "✦ &nbsp;Notes to help you talk with your doctor if needed</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    user = auth.get_current_user()
    if user:
        st.caption(f"Signed in as {user['email']}")

    if st.button("Let's get started →", type="primary", use_container_width=True):
        go_to("profile")


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
            help="First name only — used to personalise your screen. Never stored in files or sent to AI services.",
        )
        st.caption("First name only — used on-screen during this session. Not stored in files or sent to AI services.")

        age_months = st.number_input(
            "Age in months *",
            min_value=2, max_value=72, value=24, step=1,
            help="Count from birth. A 2-year-old is around 24 months.",
        )

        diagnosis = st.text_input(
            "Does your child have a diagnosis or condition? (optional)",
            placeholder="e.g. none, not sure, speech delay, autism, developmental delay",
        )

        concern = st.text_area(
            "What is your main concern about your child's development? *",
            placeholder="Describe what you've noticed or what you'd like support with.",
            height=100,
        )

        daily_time = st.number_input(
            "How many minutes a day can you spend on activities with your child? *",
            min_value=5, max_value=60, value=15, step=5,
        )

        submitted = st.form_submit_button(
            "Start the questions →", type="primary", use_container_width=True
        )

    if submitted:
        errors = []
        if not child_name.strip():
            errors.append("Please enter your child's first name.")
        if not concern.strip():
            errors.append("Please describe your main concern.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            # Store display name in session state only — never pass to engine
            st.session_state["child_display_name"] = child_name.strip()
            st.session_state["child_concern"]      = concern.strip()
            st.session_state["session_id"]         = str(uuid.uuid4())
            st.session_state["child_id"]           = str(uuid.uuid4())

            # Pass "your child" as the name so it never reaches OpenAI
            state = init_state_from_profile(
                name="your child",
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
    st.session_state["genex_state"]         = state
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
    domain_idx  = st.session_state.get("interview_domain_idx", 0)
    child_name  = get_child_display_name()

    # All domains done → build plan
    if domain_idx >= len(domain_keys):
        st.markdown(
            """
            <div style="display:flex;flex-direction:column;align-items:center;
                        justify-content:center;min-height:65vh;text-align:center;padding:2rem">
                <div style="font-size:3.5rem;margin-bottom:1rem">🌱</div>
                <h2 style="font-size:1.8rem;font-weight:700;color:#5B21B6;margin:0 0 0.5rem">
                    Building your personalised plan…
                </h2>
                <p style="color:#6B7280;font-size:1.05rem;margin:0">This takes just a moment.</p>
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
        st.session_state["genex_state"]  = state
        st.session_state.pop("parent_plan", None)
        st.session_state["plan_just_built"] = True
        save_session_json(state)
        go_to("weekly_plan")
        return

    category_key = domain_keys[domain_idx]
    parent_label = DOMAIN_LABELS.get(category_key, DOMAIN_CONFIG[category_key]["display"])
    icon         = DOMAIN_ICONS.get(category_key, "📋")

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

    # Header
    _render_logo(height=48)
    st.markdown(f"## {icon} {parent_label}")

    # Domain progress dots
    dots = ""
    for i, dk in enumerate(domain_keys):
        dots += ("🟣 " if i < domain_idx else "⚪ " if i == domain_idx else "○ ")
    st.markdown(
        f"<span style='font-size:0.85rem;color:#7C3AED'>{dots.strip()}</span>",
        unsafe_allow_html=True,
    )

    band_pct = int((band_idx / max(len(band_months), 1)) * 100)
    st.progress(band_pct)
    st.markdown("")

    st.markdown(
        "<div class='genex-card' style='background:#F5F0FF;padding:0.75rem 1rem;"
        "margin-bottom:0.75rem;border:1px solid #DDD6FE'>"
        "<p style='margin:0;font-size:0.92rem;color:#5B21B6'>"
        "There are no right or wrong answers. "
        "This helps Genex understand what kind of support may be most helpful for your child."
        "</p></div>",
        unsafe_allow_html=True,
    )

    cache_key = f"bc_{category_key}_{current_month}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = {}

    with st.form(f"band_form_{category_key}_{current_month}"):
        responses   = {}
        answer_keys = list(ANSWER_OPTIONS.keys())

        for q in questions:
            milestone_text = q["milestone"].rstrip(".?")
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
                index=st.session_state[cache_key].get(q["question_id"], 0),
                key=f"bq_{q['question_id']}",
                horizontal=True,
                label_visibility="collapsed",
            )
            responses[q["question_id"]] = sel
            st.markdown("")

        submitted = st.form_submit_button(
            "Next →", type="primary", use_container_width=True
        )

    if submitted:
        for qid, sel in responses.items():
            st.session_state[cache_key][qid] = answer_keys.index(sel)
        for q in questions:
            record_answer(state, category_key, q,
                          ANSWER_OPTIONS[responses[q["question_id"]]])
        st.session_state["genex_state"] = state

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

    # Back navigation
    st.markdown("")
    col_back, _ = st.columns([1, 3])
    with col_back:
        if band_idx > 0:
            if st.button("← Back", key="back_band"):
                prev_month = band_months[band_idx - 1]
                n = len(bands[prev_month])
                if state["qna"].get(category_key):
                    state["qna"][category_key] = state["qna"][category_key][:-n]
                st.session_state["genex_state"]         = state
                st.session_state[f"bi_{category_key}"]  = band_idx - 1
                st.session_state[f"bf_{category_key}"]  = 0
                st.rerun()
        elif domain_idx > 0:
            if st.button("← Back", key="back_domain"):
                prev_key = domain_keys[domain_idx - 1]
                _clear_domain_band_state(category_key)
                state["qna"][category_key] = []
                _clear_domain_band_state(prev_key)
                state["qna"][prev_key] = []
                st.session_state["genex_state"]          = state
                st.session_state["interview_domain_idx"] = domain_idx - 1
                st.rerun()


# ── Activity helpers ───────────────────────────────────────────────────────

def _build_why_helps(activity: dict, state: dict) -> str:
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
    return WHY.get(activity.get("category_key", ""),
                   "This activity supports your child's development in a meaningful way.")


def _render_activity_detail(item: dict, child_name: str, key_prefix: str = ""):
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


def _get_activity_bank_flat(state: dict) -> list:
    result = []
    for category_key in DOMAIN_CONFIG:
        bank = state.get("activity_banks", {}).get(category_key, {})
        for a in bank.get("activities", []):
            result.append({**a, "category_key": category_key})
    return result


# ── Screen: Weekly Plan ────────────────────────────────────────────────────

def screen_weekly_plan():
    progress_bar()

    state = get_state()
    if not state.get("weekly_schedule"):
        st.warning("No plan yet. Please complete the questions first.")
        if st.button("← Back"):
            go_to("interview")
        return

    child_name = get_child_display_name()
    schedule   = state["weekly_schedule"]

    if "parent_plan" not in st.session_state:
        st.session_state["parent_plan"] = copy.deepcopy(schedule.get("days", {}))

    plan: dict = st.session_state["parent_plan"]

    _render_logo(height=52)

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
    st.caption("Tap any activity to see details. Remove or add activities to suit your week.")
    st.markdown("")

    WEEKDAYS  = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    WEEKEND   = ["Saturday", "Sunday"]
    ALL_DAYS  = WEEKDAYS + WEEKEND
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
                item      = plan[day]["items"][idx]
                icon      = DOMAIN_ICONS.get(item.get("category_key", ""), "🌱")
                col_exp, col_del = st.columns([11, 1])
                with col_exp:
                    with st.expander(
                        f"{icon} **{item.get('title', '')}** — {item.get('duration_min', 5)} min",
                        expanded=False,
                    ):
                        _render_activity_detail(item, child_name, key_prefix=f"{day}_{idx}")
                with col_del:
                    if st.button("✕", key=f"remove_{day}_{idx}", help="Remove this activity"):
                        plan[day]["items"].pop(idx)
                        st.rerun()

        # Add from bank — available every day
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

    # Download (no child name in filename)
    session_id = st.session_state.get("session_id", "plan")
    plan_text  = _build_plan_text_from(plan, child_name)
    st.download_button(
        label="⬇️ Download Weekly Plan",
        data=plan_text,
        file_name=f"genex_plan_{datetime.now().strftime('%Y%m%d')}.txt",
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
    lines = [
        "Genex Parent Copilot — Weekly Plan",
        f"Child: {child_name}",
        f"Generated: {datetime.now().strftime('%B %d, %Y')}",
        "=" * 50,
        "",
        "This plan is for home support only.",
        "It does not replace professional advice.",
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
    return "\n".join(lines)


# ── Screen: Doctor Notes ───────────────────────────────────────────────────

def screen_doctor_prep():
    progress_bar()

    state = get_state()
    if not state.get("dev_age"):
        st.warning("Please complete the questions first.")
        return

    _render_logo(height=52)
    st.markdown("## 🩺 Notes for Your Doctor")
    st.caption(
        "These are conversation starters — things you might want to bring up at your child's "
        "next appointment. Genex does not make diagnoses."
    )
    st.markdown("")

    prep       = build_doctor_visit_prep(state)
    child_name = get_child_display_name()

    if prep.get("priority_domains"):
        st.markdown(
            "<div class='genex-doctor-card'>"
            "<div class='genex-section-label' style='color:#4A6080'>Areas worth discussing</div>",
            unsafe_allow_html=True,
        )
        for r in prep["priority_domains"]:
            parent_label = DOMAIN_LABELS.get(
                next((k for k,v in DOMAIN_CONFIG.items() if v["display"]==r["category"]), ""),
                r["category"],
            )
            st.markdown(
                f"<div class='genex-section' style='margin:0.5rem 0'>"
                f"<p style='font-weight:600;margin:0 0 0.3rem'>{parent_label}</p>"
                f"<p style='margin:0;font-size:0.93rem'>"
                f"Based on your answers, this is an area that may be worth mentioning to your doctor."
                f"</p></div>",
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    if prep.get("monitor_domains"):
        st.markdown("")
        st.markdown(
            "<div class='genex-section-label' style='color:#4A6080'>Worth keeping an eye on</div>",
            unsafe_allow_html=True,
        )
        for r in prep["monitor_domains"]:
            parent_label = DOMAIN_LABELS.get(
                next((k for k,v in DOMAIN_CONFIG.items() if v["display"]==r["category"]), ""),
                r["category"],
            )
            st.markdown(f"- **{parent_label}**")

    st.markdown("")
    st.markdown(
        "<div class='genex-card'>"
        "<div class='genex-section-label'>Questions to ask your pediatrician or specialist</div>",
        unsafe_allow_html=True,
    )
    for q in prep.get("questions_for_doctor", []):
        q_personal = q.replace("the child", child_name).replace("your child", child_name)
        st.markdown(
            f"<p style='margin:0.4rem 0;font-size:0.95rem'>❓ {q_personal}</p>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

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


# ── Screen: Feedback ───────────────────────────────────────────────────────

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
    user  = auth.get_current_user()

    with st.form("feedback_form"):
        overall = st.radio(
            "Overall, how useful did you find Genex?",
            options=["Very useful", "Somewhat useful", "Not very useful", "Not useful at all"],
            horizontal=True,
        )
        activity_rating = st.radio(
            "How relevant did the weekly plan feel for your child?",
            options=["Very relevant", "Somewhat relevant", "Not very relevant", "Not sure"],
            horizontal=True,
        )
        language_rating = st.radio(
            "Was the language clear and easy to understand?",
            options=["Yes, very clear", "Mostly clear", "Sometimes confusing", "Hard to understand"],
            horizontal=True,
        )
        st.markdown("")
        what_helped  = st.text_area("What was most helpful?", height=80,
                                    placeholder="Anything you found useful or reassuring…")
        what_change  = st.text_area("What would you change or improve?", height=80,
                                    placeholder="Anything confusing, missing, or that didn't feel right…")
        general      = st.text_area("Anything else you'd like to share?", height=80,
                                    placeholder="Open comments…")
        submitted = st.form_submit_button(
            "Submit Feedback", type="primary", use_container_width=True
        )

    if submitted:
        try:
            session_id = st.session_state.get("session_id", str(uuid.uuid4()))
            user_id    = user["uid"] if user else "anonymous"
            stamp      = datetime.now(timezone.utc).isoformat()

            feedback = {
                "user_id":     user_id,
                "session_id":  session_id,
                # No child name — only age and diagnosis stored
                "age_months":             child.get("chronological_months"),
                "diagnosis_or_condition": child.get("diagnosis"),
                "ratings": {
                    "overall_usefulness": overall,
                    "activity_relevance": activity_rating,
                    "language_clarity":   language_rating,
                },
                "comments": {
                    "what_helped":    what_helped,
                    "what_to_change": what_change,
                    "general":        general,
                },
                "submitted_at":  stamp,
                "app_version":   APP_VERSION,
                "engine_version": ENGINE_VERSION,
            }
            blob_name      = f"feedback/{user_id}/{session_id}_feedback.json"
            local_fb_dir   = FEEDBACK_DIR / user_id
            local_fb_dir.mkdir(parents=True, exist_ok=True)
            result         = _storage_save(feedback, blob_name, local_fb_dir)

            if result == "gcs":
                from genex_core.storage import GCS_BUCKET_NAME
                saved_at = f"gs://{GCS_BUCKET_NAME}/{blob_name}"
                st.success(
                    f"✅ Thank you — your feedback has been saved.\n\n"
                    f"📍 Saved to: `{saved_at}`"
                )
            elif result == "local":
                local_path = local_fb_dir / f"{session_id}_feedback.json"
                st.success(
                    f"✅ Thank you — your feedback has been saved.\n\n"
                    f"📍 Saved locally: `{local_path}`"
                )
            else:
                st.warning("Feedback could not be saved automatically. Please screenshot this page.")
        except Exception as exc:
            st.error(f"Could not save feedback: {exc}")

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

        for s in MAIN_SCREENS:
            label = SCREEN_LABELS[s]
            if st.button(label, key=f"nav_{s}", use_container_width=True):
                go_to(s)

        state = get_state()
        if state.get("child"):
            child = state["child"]
            st.divider()
            st.caption(
                f"**{get_child_display_name()}** · "
                f"{child.get('chronological_months', '—')} months"
            )

        user = auth.get_current_user()
        if user:
            st.divider()
            st.caption(f"Signed in as\n{user['email']}")
            if st.button("Sign out", use_container_width=True):
                auth.sign_out()
                st.rerun()


# ── Router ─────────────────────────────────────────────────────────────────

def main():
    # Pre-auth screens — accessible without login
    screen = st.session_state.get("screen", "login")

    if not auth.is_authenticated():
        if screen == "register":
            screen_register()
        elif screen == "reset_password":
            screen_reset_password()
        elif screen == "privacy_policy":
            screen_privacy_policy()
        else:
            st.session_state["screen"] = "login"
            screen_login()
        return

    # Authenticated — show sidebar + top auth bar, then route
    sidebar_nav()
    _render_auth_header()
    screen = current_screen()

    if screen == "privacy_policy":
        screen_privacy_policy()
    elif screen == "welcome":
        screen_welcome()
        _render_footer()
    elif screen == "profile":
        screen_profile()
        _render_footer()
    elif screen == "interview":
        screen_interview()
    elif screen == "weekly_plan":
        screen_weekly_plan()
        _render_footer()
    elif screen == "doctor_prep":
        screen_doctor_prep()
        _render_footer()
    elif screen == "feedback":
        screen_feedback()
        _render_footer()
    else:
        go_to("welcome")


if __name__ == "__main__":
    main()
