"""Provisioning errors. PHI-safe messages, no account identifiers.

Subject-collision outcomes deliberately reuse `integration/errors.py`
(`SubjectAlreadyHeld`, `AmbiguousSubjectState`) rather than defining provider
twins of them: "this subject already belongs to someone else" is ONE condition
regardless of which side discovered it, and a caller handling the caregiver
path must not have to catch a second exception type to handle the provider one.

Only genuinely provisioning-specific failures live here.
"""

from __future__ import annotations

from ..integration.errors import IntegrationError


class ProviderProvisioningError(IntegrationError):
    """Invalid provisioning request — bad practice, discipline or name.

    Carries no `http_status`: the transport layer owns that mapping, exactly as
    it does for the 0.5A integration errors. `code` exists only to be recorded
    as audit `integration_state`, which is a short enum and not a message.
    """

    code = "PROVIDER_PROVISIONING_INVALID"


class ProviderProvisioningConflict(IntegrationError):
    """The subject is provisioned, but not as the caller described it.

    Distinct from `SubjectAlreadyHeld`: the subject IS this provider, so there
    is no identity collision. What conflicts is the practice of record, which
    cannot be changed by re-provisioning because `ProviderChildConnection` and
    `ManagingClinicianAssignment` have already denormalised it.
    """

    code = "PROVIDER_PROVISIONING_CONFLICT"
