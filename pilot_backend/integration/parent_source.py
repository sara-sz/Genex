"""pilot_backend/integration/parent_source.py — READ-ONLY Parent boundary.

    ParentSessionSource   the port
    ParentSessionFacts    the only thing that crosses it

## Ports not SDKs, again

`pilot_backend` must not import the Parent API, `google.cloud.storage`, or
anything that knows what GCS is. The port is a Protocol here; the adapter that
actually reads `sessions/{uid}/{session_id}.json` lives in `pilot_runtime`,
exactly as `DocumentStore` and `TokenDecoder` do.

Parent 2.3 is not modified, not imported, and never written to.

## The port returns FACTS, not the session

`ParentSessionFacts` carries three things: the session id, the owner uid, and
whether the session exists at all. No plan, no answers, no feedback, no notes,
no diagnosis, no concern — because 0.5A is an IDENTITY bridge, and a port that
could return clinical content is a port a later slice will quietly use for it.

A `Child` in the pilot carries no name, age or clinical field, so there is
nothing further the bridge legitimately needs.

## Ownership is verified server-side, in ONE place, with defence in depth

Parent stores a session at `sessions/{uid}/{session_id}.json`, and its
`_require_session` 404s on a missing blob then 403s when
`doc["owner_uid"] != uid` — a second check even though it already read from
that uid's own prefix. Both layers are reproduced here, and the pilot keeps the
decision in one place:

  1. **Path scoping.** The blob name is built from the REQUESTING subject, so
     the adapter cannot read another account's prefix at all. That is the only
     reason `fetch_session_facts` takes `requesting_subject` — the identifier
     locates the document, it asserts nothing.
  2. **Document agreement.** The adapter returns the `owner_uid` the document
     itself carries, verbatim and unjudged.
  3. **The service decides.** `IntegrationIdentityService` compares that value
     against the VERIFIED token subject and refuses on mismatch.

Layer 3 is not redundant with layer 1. A document sitting under one prefix
while naming a different `owner_uid` is a genuine inconsistency — precisely
what Parent's own second check exists to catch — and it is refused rather than
trusted for being in the right folder.

Letting the SERVICE decide, rather than having the adapter return None for a
mismatch, is what keeps "absent" and "not yours" converging at one place
instead of drifting into two caller-visible outcomes. See `errors.py` for why
distinguishing them would make this an enumeration oracle.

`requesting_subject` never comes from a request body or query string. It is the
verified token subject, passed down by the service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ParentSessionFacts:
    """The minimum a canonical-identity bridge needs from a Parent session.

    Deliberately three fields. Anything a later slice needs from a Parent
    session must be added here explicitly, which makes the widening of this
    boundary a reviewable edit rather than an accident.
    """

    #: The source-system identifier. NEVER the canonical child id.
    session_id: str
    #: The Firebase uid Parent recorded as the owner.
    owner_uid: str

    def __post_init__(self) -> None:
        if not (self.session_id or "").strip():
            raise ValueError("session facts require a session id")
        if not (self.owner_uid or "").strip():
            raise ValueError("session facts require an owner uid")

    def is_owned_by(self, auth_subject: str) -> bool:
        """Exact match on the Firebase uid. Case-sensitive, like the uid."""
        return self.owner_uid == (auth_subject or "").strip()


@runtime_checkable
class ParentSessionSource(Protocol):
    """Read one Parent session's identity facts. No write operation exists.

    There is no `save`, `update` or `delete` in this protocol, so an adapter
    satisfying it cannot mutate Parent state even by mistake — the capability
    is absent from the contract rather than merely unused.
    """

    def fetch_session_facts(self, session_id: str, *, requesting_subject: str
                            ) -> Optional[ParentSessionFacts]:
        """Facts for `session_id` within `requesting_subject`'s own namespace.

        Returns None when no such session exists THERE. Returning None rather
        than raising keeps the ownership decision in ONE place — the service —
        so "missing" and "not yours" cannot diverge into two different
        caller-visible outcomes.

        `requesting_subject` scopes the lookup; it must never be substituted
        for the document's own `owner_uid` in the returned facts.
        """
        ...


class InMemoryParentSessionSource:
    """Fictional Parent sessions for tests and the 0.5A fictional pilot.

    Not a stub standing in for a missing adapter: the real GCS adapter lives
    in `pilot_runtime/integration/`, and this exists so `pilot_backend` tests
    stay dependency-pure, matching how `FakeDocumentStore` relates to
    `FirestoreDocumentStore`.
    """

    def __init__(self, sessions: Optional[dict] = None) -> None:
        #: session_id -> owner_uid
        self._sessions = dict(sessions or {})
        #: (prefix_subject, session_id) -> owner_uid, for the inconsistent case
        #: the GCS adapter can encounter: a document filed under one account
        #: while naming another as its owner.
        self._misfiled: dict = {}

    def add(self, session_id: str, owner_uid: str) -> None:
        self._sessions[session_id] = owner_uid

    def add_misfiled(self, session_id: str, *, prefix_subject: str,
                     owner_uid: str) -> None:
        """A session readable under `prefix_subject` but owned by someone else.

        Exists so the service's ownership check is exercised on the shape the
        real adapter can actually produce — not only on a session that is
        absent from the caller's namespace.
        """
        self._misfiled[(prefix_subject, session_id)] = owner_uid

    def fetch_session_facts(self, session_id: str, *, requesting_subject: str
                            ) -> Optional[ParentSessionFacts]:
        key = (session_id or "").strip()
        subject = (requesting_subject or "").strip()

        misfiled = self._misfiled.get((subject, key))
        if misfiled is not None:
            return ParentSessionFacts(session_id=key, owner_uid=misfiled)

        owner = self._sessions.get(key)
        if owner is None:
            return None
        # Path scoping, as GCS gives for free: a session in somebody else's
        # namespace is simply not visible here.
        if owner != subject:
            return None
        return ParentSessionFacts(session_id=key, owner_uid=owner)
