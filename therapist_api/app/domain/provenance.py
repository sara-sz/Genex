"""Progress provenance states (correction #5).

Genex is home-support, NOT medical advice/diagnosis. Progress is described by
where the signal came from — never by clinical confirmation.

Allowed provenance:
  * parent_reported     — the parent reported an observation
  * parent_confirmed    — the parent confirmed after practice (milestone check-in)
  * therapist_reviewed  — a therapist reviewed parent-provided information

IMPORTANT:
  * "parent_confirmed" is displayed as "Parent-confirmed" — it is NEVER
    collapsed into a bare "Confirmed".
  * Clinical-confirmation language is forbidden anywhere in the product.
"""

from __future__ import annotations

from enum import Enum


class Provenance(str, Enum):
    PARENT_REPORTED = "parent_reported"
    PARENT_CONFIRMED = "parent_confirmed"
    THERAPIST_REVIEWED = "therapist_reviewed"


# Display labels are explicit and non-clinical. Note the retained "Parent-" prefix.
PROVENANCE_DISPLAY = {
    Provenance.PARENT_REPORTED: "Parent-reported",
    Provenance.PARENT_CONFIRMED: "Parent-confirmed",
    Provenance.THERAPIST_REVIEWED: "Therapist-reviewed",
}

# Progress phrasing allowed alongside provenance (home-support, non-clinical).
ALLOWED_PROGRESS_LABELS = ("Parent-reported", "Parent-confirmed", "Practicing", "Emerging")

# Language that must never appear as a progress/provenance label.
FORBIDDEN_PROGRESS_LABELS = (
    "Clinically confirmed",
    "Clinically Confirmed",
    "Confirmed",  # bare "Confirmed" is forbidden — must stay "Parent-confirmed"
    "Mastered",
    "Passed",
    "Failed",
)


def display_label(provenance: Provenance) -> str:
    return PROVENANCE_DISPLAY[provenance]
