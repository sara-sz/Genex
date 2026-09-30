"""pilot_backend/identity/errors.py — identity failure types.

All three are PHI-safe by declaration: their messages name the rule that
failed and, at most, an opaque application id. None ever carries an external
identifier, a person, or clinical content.
"""

from __future__ import annotations


class IdentityConflict(Exception):
    """A uniqueness or ambiguity rule refused the write.

    Raised when an active mapping already exists, when a competing writer won
    the claim, or when a key resolves to more than one record. Always a
    refusal — never resolved by picking one.
    """

    PHI_SAFE_MESSAGE = True


class IdentityAuthorizationError(Exception):
    """The caller is authenticated but not permitted this identity operation.

    Maps to 403. Distinct from `IdentityConflict` so a permission failure can
    never be reported as a data problem, or the reverse.
    """

    PHI_SAFE_MESSAGE = True


class IdentityValidationError(Exception):
    """Inputs are internally inconsistent — e.g. practice/provider mismatch."""

    PHI_SAFE_MESSAGE = True
