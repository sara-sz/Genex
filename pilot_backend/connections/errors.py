"""Connection-lifecycle errors. PHI-safe, and deliberately non-enumerating.

The important design choice here is what these errors DO NOT distinguish.

`ProviderNotConnectable` is raised for an unknown `provider_id`, a retired one,
and one belonging to no active practice — all three collapse to one outcome
with one message. Separating them would turn the invite endpoint into an oracle
for the provider id space: a caller could walk `prov_` ids and learn which
exist from the error alone. Knowing a provider exists is not itself harmful,
but being able to enumerate clinicians is, and 0.5B has no provider directory
precisely so that enumeration is impossible.

`ConnectionNotFound` is raised both when a connection id does not exist and
when it exists but belongs to somebody else's child or another provider. Same
reasoning as `authorize_child_access`: "not yours" and "not real" must be
indistinguishable, or the id becomes a probe.
"""

from __future__ import annotations

from ..integration.errors import IntegrationError


class ProviderNotConnectable(IntegrationError):
    """No provider a caregiver may connect to under this identifier.

    ONE error for absent, inactive and not-in-an-active-practice, by design —
    see the module docstring.
    """

    code = "PROVIDER_NOT_CONNECTABLE"


class ConnectionNotFound(IntegrationError):
    """No connection this caller may act on under this identifier.

    Covers absent and not-yours identically.
    """

    code = "CONNECTION_NOT_FOUND"


class DuplicateLiveConnection(IntegrationError):
    """A live connection already exists for this (provider, child).

    Raised when the PROVIDER_CONNECTION claim is already held — either because
    one genuinely exists, or because a competing writer won the race a moment
    ago. The caller cannot tell those apart and does not need to: in both cases
    the correct next step is to read the existing connection rather than create
    a second one.
    """

    code = "DUPLICATE_LIVE_CONNECTION"


class ConnectionStateConflict(IntegrationError):
    """The transition is not legal from the connection's current state.

    Distinct from `ConnectionNotFound`: the caller is entitled to see this
    connection, so naming the illegal transition leaks nothing. Accepting an
    already-declined invitation and pausing a pending one both land here.
    """

    code = "CONNECTION_STATE_CONFLICT"
