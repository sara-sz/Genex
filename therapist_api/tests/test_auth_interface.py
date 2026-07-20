"""Auth interface fails closed by default; stub enforces env isolation."""

from __future__ import annotations

import pytest

from app.auth import (
    AuthenticatedUser,
    AuthError,
    FailClosedAuthVerifier,
    StubAuthVerifier,
)
from app.domain.roles import UserRole


def test_default_verifier_fails_closed():
    v = FailClosedAuthVerifier("dev")
    with pytest.raises(AuthError):
        v.verify("any-token")


def test_stub_rejects_missing_and_unknown_token():
    v = StubAuthVerifier("dev")
    with pytest.raises(AuthError):
        v.verify(None)
    with pytest.raises(AuthError):
        v.verify("nope")


def test_stub_accepts_known_token():
    user = AuthenticatedUser(uid="u1", environment="dev", role=UserRole.SLP)
    v = StubAuthVerifier("dev", {"tok": user})
    got = v.verify("tok")
    assert got.uid == "u1"
    assert got.role == UserRole.SLP


def test_dev_token_rejected_by_prod_service():
    dev_user = AuthenticatedUser(uid="u1", environment="dev", role=UserRole.SLP)
    prod_verifier = StubAuthVerifier("prod", {"tok": dev_user})
    with pytest.raises(AuthError):
        prod_verifier.verify("tok")


def test_prod_token_rejected_by_dev_service():
    prod_user = AuthenticatedUser(uid="u1", environment="prod", role=UserRole.SLP)
    dev_verifier = StubAuthVerifier("dev", {"tok": prod_user})
    with pytest.raises(AuthError):
        dev_verifier.verify("tok")
