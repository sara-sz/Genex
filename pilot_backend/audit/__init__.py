"""pilot_backend.audit — generic, PHI-free audit events.

Only the pure event model is re-exported here. `AuditRecorder` deliberately is
NOT: it depends on `pilot_backend.persistence`, which in turn needs
`audit.events` for its codecs. Importing the recorder from this `__init__`
would close that loop and make `import pilot_backend.persistence` fail
depending on which package a caller happened to touch first.

    from pilot_backend.audit.recorder import AuditRecorder
"""

from .events import (
    ALLOWED_METADATA_KEYS,
    AuditAction,
    AuditEvent,
    AuditMetadataError,
    AuditResult,
)

__all__ = [
    "AuditAction",
    "AuditEvent",
    "AuditResult",
    "AuditMetadataError",
    "ALLOWED_METADATA_KEYS",
]
