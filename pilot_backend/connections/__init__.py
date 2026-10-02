"""Provider-child connection lifecycle — caregiver-initiated only in 0.5B.

Deliberately NOT in this slice: provider-to-family invitation and redemption,
family search, a provider directory, and public provider self-registration.
The Tuesday pilot needs one direction — a family connecting a clinician they
already know — and every deferred item above is a surface that can leak which
families or clinicians exist. None of them is required to connect Hannah to one
child, so none of them is built.
"""

from .errors import (
    ConnectionNotFound,
    ConnectionStateConflict,
    DuplicateLiveConnection,
    ProviderNotConnectable,
)
from .service import ProviderConnectionService

__all__ = [
    "ConnectionNotFound",
    "ConnectionStateConflict",
    "DuplicateLiveConnection",
    "ProviderConnectionService",
    "ProviderNotConnectable",
]
