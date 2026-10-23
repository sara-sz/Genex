"""The opaque browser-visible question reference.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## WHY `skill_key` COULD NOT STAY IN THE BROWSER

`skill_key` is `domain · subdomain · months · milestone` joined by `U+001F`. A
client that splits it on that separator recovers the canonical developmental
MONTH and the SUBDOMAIN that `_client_question` deliberately withholds. Telling
clients to treat it as opaque does not make it opaque; the month still arrives.
That fails the frozen no-developmental-month exposure rule.

So the browser gets a `question_ref` and the canonical `skill_key` stays
server-side.

## THE DESIGN, AND THE TWO IT WAS CHOSEN OVER

`question_ref = sha256(session_salt || skill_key)[:32]`, with ONE random salt
per baseline session held in the lifecycle ENVELOPE.

A PURE derivation with no salt was rejected. The canonical inventory is small —
163 distinct rungs across the whole workbook — so `sha256(skill_key)` is a
163-entry lookup table for anyone holding the public taxonomy. That is easier
than parsing, not harder, and would have moved the leak rather than closed it.

A PER-QUESTION STORED MAPPING was rejected as larger. It grows with the
interview, needs its own reload and replay handling, and gives a token that has
to stay valid across a session restore. One salt is O(1), is written once, and
makes the reference DETERMINISTIC — which is what buys replay and idempotency
for free: re-asking the same question yields the same reference, and a client
retrying with a stale reference still mismatches exactly as before.

A NEW DEPLOYMENT SECRET was rejected as the largest. It would need an env var
set on two Parent services before activation, and an unset-secret path that
fails closed; the salt needs none of that.

## WHAT IT DOES AND DOES NOT PROTECT

The reference is a digest, so months, subdomain and milestone cannot be
recovered by parsing. It is NOT a capability token and is not secret: a client
already knows which question it was just asked, and the reference identifies
exactly that. Its job is to let `/answer` prove the answer belongs to the
expected canonical question without publishing that question's identity.

Server-side validation is UNWEAKENED. `/answer` still resolves the engine's
expected question, still compares against exactly one canonical skill, and
still refuses a stale submission — it compares references instead of keys, and
a reference is injective in `skill_key` for a fixed salt.

## THE SALT LIVES IN THE ENVELOPE, NEVER IN THE RECORD

`ENVELOPE_STATE_KEY_V2` is a different document key from
`CANONICAL_STATE_KEY_V2`, and the A2 source digest is taken over the canonical
`record_to_state_v2` dict alone. So the salt cannot move a projection id, and
no historical finalized evidence changes.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, Optional

#: 32 bytes of `os.urandom` via `secrets`. Deliberately not `random`, which is
#: seeded and reproducible.
SALT_BYTES = 32

#: 128 bits of the digest, hex-encoded. Enough that a collision between two of
#: the 163 canonical rungs under one salt is not a thing that happens, and
#: short enough to read in a log without wrapping.
REF_HEX_LENGTH = 32

#: Separator between salt and key. The same unit separator the canonical key
#: uses, which cannot occur in either operand, so the concatenation is
#: unambiguous and two different pairs cannot hash the same input.
_SEP = "\x1f"

ENVELOPE_SALT_FIELD = "question_salt"


class QuestionRefError(Exception):
    """A question reference could not be produced. PHI-safe: no child data."""

    PHI_SAFE_MESSAGE = True


def new_salt() -> str:
    """A fresh per-session salt."""
    return secrets.token_urlsafe(SALT_BYTES)


def question_ref(salt: str, skill_key: str) -> str:
    """The browser-visible reference for one canonical skill.

    Deterministic in `(salt, skill_key)`, which is what makes a retry with the
    same reference behave identically and a retry with a stale one fail
    identically.
    """
    if not (salt or "").strip():
        raise QuestionRefError("a question reference needs a session salt")
    if not (skill_key or "").strip():
        raise QuestionRefError("a question reference needs a canonical key")
    digest = hashlib.sha256((salt + _SEP + skill_key).encode("utf-8"))
    return digest.hexdigest()[:REF_HEX_LENGTH]


def salt_from_envelope(envelope: Optional[Dict[str, Any]]) -> Optional[str]:
    value = (envelope or {}).get(ENVELOPE_SALT_FIELD)
    return value if isinstance(value, str) and value.strip() else None


def matches(salt: str, skill_key: str, candidate_ref: str) -> bool:
    """Whether `candidate_ref` is the reference for this canonical skill.

    A plain equality on the derived value. Not `secrets.compare_digest`: the
    reference is not a secret and not a capability — the client was just served
    it — so there is no timing channel worth defending, and pretending
    otherwise would misdescribe what this value is.
    """
    return question_ref(salt, skill_key) == (candidate_ref or "")


__all__ = [
    "ENVELOPE_SALT_FIELD", "REF_HEX_LENGTH", "SALT_BYTES",
    "QuestionRefError", "new_salt", "question_ref", "salt_from_envelope",
    "matches",
]
