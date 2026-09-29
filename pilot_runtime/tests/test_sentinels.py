"""Fictional sentinel strings shared by the PRE-PHI 0.3 suites.

Stand-ins for the categories of content that must never reach a response, a
log line or an audit document. Deliberately unmistakable, so a test that finds
one has found a real leak rather than a coincidence.

There is no real PHI anywhere in this repository. `Child` carries no clinical
field and `ChildContextRecord` holds only an opaque content reference, so
these sentinels represent what WOULD be sensitive in production rather than
anything the system actually stores today.

Not a test module despite the name pattern — it contains no tests, only the
constants the test modules import.
"""

from __future__ import annotations

SENTINEL_CHILD_NAME = "ZZSENTINEL-CHILDNAME-Quillwood"
SENTINEL_NOTE = "ZZSENTINEL-NOTE-refuses-solids-at-dinner"
SENTINEL_DIAGNOSIS = "ZZSENTINEL-DIAGNOSIS-fictional-condition"
SENTINEL_CONCERN = "ZZSENTINEL-CONCERN-not-speaking-in-sentences"
SENTINEL_TOKEN = "ZZSENTINEL-TOKEN-abc123def456"
SENTINEL_SECRET = "ZZSENTINEL-SECRET-xyz789"
SENTINEL_EMAIL = "zzsentinel-caregiver@example.invalid"

ALL_SENTINELS = (
    SENTINEL_CHILD_NAME,
    SENTINEL_NOTE,
    SENTINEL_DIAGNOSIS,
    SENTINEL_CONCERN,
    SENTINEL_TOKEN,
    SENTINEL_SECRET,
    SENTINEL_EMAIL,
)
