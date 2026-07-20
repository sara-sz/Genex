"""Developmental-domain taxonomy: 8 labels accepted; unknown is explicit."""

from __future__ import annotations

import pytest

from app.domain.domains import (
    DISPLAY_DOMAINS,
    UnknownDomainError,
    is_valid_domain,
    require_known_domain,
)


def test_exactly_eight_labels():
    assert DISPLAY_DOMAINS == (
        "Talking & Communicating",
        "Social & Emotional",
        "Learning & Thinking",
        "Movement & Physical",
        "Daily Living",
        "Sensory",
        "Fine Motor",
        "Gross Motor",
    )


def test_all_eight_labels_accepted():
    for label in DISPLAY_DOMAINS:
        assert is_valid_domain(label)
        assert require_known_domain(label) == label


def test_unknown_domain_is_explicit():
    assert not is_valid_domain("Speech")  # not a display label
    with pytest.raises(UnknownDomainError):
        require_known_domain("Speech")


def test_unknown_domain_error_lists_taxonomy():
    with pytest.raises(UnknownDomainError) as e:
        require_known_domain("bogus")
    assert "genex-parent-display-v1" in str(e.value)
