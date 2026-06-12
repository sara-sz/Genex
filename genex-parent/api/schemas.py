"""
api/schemas.py — Pydantic request and response models for the Genex API.

All user-facing request models validate inputs strictly so that bad data
never reaches the brain pipeline. 422 responses are returned automatically
by FastAPI when validation fails.
"""

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator

# ── Valid enumerated values ────────────────────────────────────────────────

EnjoymentLiteral  = Literal["loved_it", "it_was_okay", "not_really"]
DifficultyLiteral = Literal["too_easy", "just_right", "too_hard"]
CompletionLiteral = Literal["did_it", "didnt_want_to_try", "wasnt_ready_yet"]
CareTeamLiteral   = Literal["Doctor", "ST", "OT", "PT"]
ReportTypeLiteral = Literal[
    "doctor",
    "speech_therapist",
    "occupational_therapist",
    "physical_therapist",
]


# ── Diagnosis literal — exact Lovable dropdown values ─────────────────────

DiagnosisLiteral = Literal[
    "No known diagnosis / not sure",
    "Down syndrome",
    "ADHD",
    "Autism spectrum",
    "Other",
    "Prefer not to say",
]


# ─────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────

class SessionStartRequest(BaseModel):
    child_name: str = Field(
        ...,
        description=(
            "Display name used by Lovable locally only. "
            "Never stored in GCS or sent to OpenAI."
        ),
        min_length=1,
        max_length=100,
    )
    age_years: int = Field(..., ge=0, le=10)
    age_months: int = Field(..., ge=0, le=11)
    age_in_months: int = Field(
        ...,
        description="Must equal age_years * 12 + age_months. Lovable computes this.",
        ge=0,
        le=131,
    )
    diagnosis_or_condition: DiagnosisLiteral
    parent_concern: str = Field(
        ...,
        description="Free-text parent concern. Child name will be sanitised before storage.",
        max_length=2000,
    )
    daily_time_minutes: int = Field(
        ...,
        description="Minutes per day for activities. Must be >= 5.",
        ge=5,
    )
    timezone: str = Field(
        default="UTC",
        description=(
            "IANA timezone string from Lovable: "
            "Intl.DateTimeFormat().resolvedOptions().timeZone. "
            "Used to anchor the planning week to Monday–Sunday in the parent's local timezone. "
            "Example: 'America/Los_Angeles'. Falls back to UTC if invalid or absent."
        ),
        min_length=1,
        max_length=100,
    )

    @model_validator(mode="after")
    def check_age_consistency(self) -> "SessionStartRequest":
        expected = self.age_years * 12 + self.age_months
        if self.age_in_months != expected:
            raise ValueError(
                f"age_in_months ({self.age_in_months}) must equal "
                f"age_years * 12 + age_months "
                f"({self.age_years} * 12 + {self.age_months} = {expected})."
            )
        return self


class AnswerRequest(BaseModel):
    question_id: str = Field(..., min_length=1)
    answer: Literal["yes", "sometimes", "with_help", "no", "not_sure"]


class FeedbackRequest(BaseModel):
    """
    One activity feedback record submitted by the parent via Lovable.

    activity_id must be the UUID `id` field from the plan_response activity card
    (not the brain's internal "v22_..." ID). Lovable should pass this through
    directly from the plan it received.
    """
    plan_id: str = Field(..., min_length=1)
    activity_id: str = Field(
        ...,
        min_length=1,
        description="UUID 'id' from the plan_response activity card.",
    )
    day: str = Field(..., min_length=1, max_length=20)
    activity_date: str = Field(
        ...,
        description="ISO-8601 date of the activity, e.g. '2026-06-15'.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    enjoyment: EnjoymentLiteral
    difficulty: DifficultyLiteral
    completion: CompletionLiteral
    discuss_with_care_team: bool = False
    care_team_member: Optional[CareTeamLiteral] = None
    note: str = Field(default="", max_length=1000)


class ReportRequest(BaseModel):
    report_type: ReportTypeLiteral


# ─────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────

class QuestionResponse(BaseModel):
    question_id: str
    question_text: str
    domain: str
    domain_label: str
    progress_index: int
    progress_total_estimate: int


class SessionStartResponse(BaseModel):
    session_id: str
    status: Literal["questions"]
    domains: List[str]
    total_questions_estimate: int
    current_question: QuestionResponse


class NextQuestionResponse(BaseModel):
    status: Literal["next_question"]
    current_question: QuestionResponse


class InterviewCompleteResponse(BaseModel):
    status: Literal["interview_complete"]
    ready_for_plan: bool
    questions_answered: int


# Union type returned by /answer — used for documentation only;
# FastAPI will serialise the correct model based on the dict returned.
AnswerResponse = NextQuestionResponse | InterviewCompleteResponse
