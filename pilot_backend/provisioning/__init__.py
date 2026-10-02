"""Provider identity provisioning — the ONLY way a Provider is created.

Separate from `integration/` because this is an administrative operation, not
Parent-system integration: it mints an app identity for a clinician who has
been approved out of band. It shares one primitive with the caregiver side,
`AuthSubjectIdentityClaim`, and shares it deliberately — "one subject, one app
actor" is a single invariant and must not have two implementations.
"""

from .service import (
    ProviderProvisioningService,
    ProvisionedProvider,
    provision_provider_record,
)

__all__ = [
    "ProviderProvisioningService",
    "ProvisionedProvider",
    "provision_provider_record",
]
