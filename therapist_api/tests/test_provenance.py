"""Progress provenance: parent_reported vs parent_confirmed; no clinical language."""

from __future__ import annotations

from app.domain.provenance import (
    ALLOWED_PROGRESS_LABELS,
    FORBIDDEN_PROGRESS_LABELS,
    Provenance,
    display_label,
)


def test_provenance_states_are_distinct():
    values = {p.value for p in Provenance}
    assert values == {"parent_reported", "parent_confirmed", "therapist_reviewed"}


def test_parent_reported_vs_parent_confirmed_labels():
    assert display_label(Provenance.PARENT_REPORTED) == "Parent-reported"
    # correction #5: parent_confirmed must NOT collapse into a bare "Confirmed".
    assert display_label(Provenance.PARENT_CONFIRMED) == "Parent-confirmed"
    assert display_label(Provenance.PARENT_CONFIRMED) != "Confirmed"


def test_therapist_reviewed_label():
    assert display_label(Provenance.THERAPIST_REVIEWED) == "Therapist-reviewed"


def test_no_forbidden_labels_are_produced():
    produced = {display_label(p) for p in Provenance} | set(ALLOWED_PROGRESS_LABELS)
    for forbidden in FORBIDDEN_PROGRESS_LABELS:
        assert forbidden not in produced


def test_allowed_labels_are_non_clinical():
    assert ALLOWED_PROGRESS_LABELS == (
        "Parent-reported",
        "Parent-confirmed",
        "Practicing",
        "Emerging",
    )
    assert "Confirmed" in FORBIDDEN_PROGRESS_LABELS
    assert "Mastered" in FORBIDDEN_PROGRESS_LABELS
