"""pilot_backend/integration/errors.py — integration failure types.

All PHI-safe by declaration: messages name the rule that failed and, at most,
an opaque application id. None carries an auth subject, an email, a session
document, a child name or any clinical field.

## The non-enumerating rule

`ParentSessionUnavailable` is raised for a session that does not exist AND for
one owned by somebody else, with the SAME message. Distinguishing them would
make the endpoint an oracle for which session ids are real — answerable by
anyone holding a valid token, which is exactly the 0.4A external-identity
lesson restated for Parent sessions.
"""

from __future__ import annotations


class IntegrationError(Exception):
    """Base for integration-boundary failures. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class IdentityBootstrapError(IntegrationError):
    """A caregiver identity could not be established."""


class SubjectAlreadyHeld(IdentityBootstrapError):
    """The auth subject already belongs to a DIFFERENT kind of app identity.

    A subject held by a Provider must never gain a second Caregiver identity:
    one person, one app identity. Fails closed rather than creating a second
    record that `get_by_auth_subject` could never disambiguate.
    """


class AmbiguousSubjectState(IdentityBootstrapError):
    """The subject already resolves to more than one app identity.

    Pre-existing damage from before write-time uniqueness existed. Deliberately
    NOT self-repaired: choosing which identity is real is a decision with
    consequences for whose data a person sees, and a bootstrap endpoint is the
    wrong place to make it silently.
    """


class ParentSessionUnavailable(IntegrationError):
    """The Parent session is absent or not owned by the caller.

    ONE error for both cases, by design — see the module docstring.
    """


class ParentSessionClaimUnusable(IntegrationError):
    """The presented handoff capability cannot be redeemed.

    ONE error covering every unusable state: no such token, expired, already
    spent, or malformed. The cases are deliberately indistinguishable.

    Separating them would turn this route into an oracle. "Expired" versus "no
    such token" tells a holder of a guessed value that the value once existed,
    and "already consumed" tells them somebody else redeemed it — which also
    reveals that the Parent session behind it is real. A single refusal
    discloses only that this attempt did not work.

    `ParentSessionUnavailable` stays separate because it means something
    different: the capability was fine and the CHILD belongs to somebody else.
    """


class ParentSessionLinkContended(IntegrationError):
    """Another writer is linking this same Parent session right now.

    A transient, retryable conflict — distinct from `SecondSessionUnresolved`,
    which is a durable product state the caller cannot retry out of. Nothing was
    persisted; a retry resolves to the winner's child through the idempotent
    path.
    """

    code = "PARENT_SESSION_LINK_CONTENDED"


class SecondSessionUnresolved(IntegrationError):
    """A different, previously unseen Parent session for the same caregiver.

    Parent is single-child today, so a second session is genuinely ambiguous:
    it could be the same child re-onboarded or a second child. Guessing either
    way is unrecoverable — merging two children, or splitting one into two.

    Nothing is persisted. Multi-child selection resolves this deliberately in
    a later slice.
    """

    code = "SECOND_SESSION_UNRESOLVED"
