"""pilot_backend/aipolicy/gate.py — no patient content leaves for an AI vendor.

## Why this exists as code and not as a policy document

The current-state audit found the Parent 2.3 staging deploy defaulting
`ACTIVITY_MODEL` to `gpt-4o-mini`, which means its AI egress path is ON unless
someone actively disables it — while an approved BAA/PHI path with the vendor
has not been verified. The presence of an API key became, in effect, the
authorization to send clinical text.

The pilot inverts that. `PHI_AI_EGRESS_DEFAULT` is False; absent configuration
denies; and a credential existing is not an input to the decision at all.

## Two independent conditions

Enabling PHI-bearing AI egress in production requires BOTH an explicit flag AND
a named BAA reference. The flag alone is one environment variable away from an
accident; requiring a reference means someone has to name the agreement that
makes it lawful, and that name is what the audit asks for later.

## What is NOT gated

Deterministic, non-AI functionality must keep working with the gate closed —
that is the whole reason Parent 0.4's functional baseline was built LLM-free.
`evaluate_phi_ai_egress` governs only PHI-bearing egress to an external model.
De-identified or non-patient uses are a separate decision and deliberately have
no code path here yet.

## Scope

This gate is the NEW pilot architecture. Frozen Parent 2.3 OpenAI behaviour is
unchanged by this phase and is not routed through here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: PHI-bearing AI egress is off unless positively, explicitly enabled.
PHI_AI_EGRESS_DEFAULT = False


class AIEgressDenied(Exception):
    """PHI-bearing AI egress was requested and refused.

    PHI-safe: states the policy reason, never the content that was to be sent.
    """

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class AIPolicy:
    """The resolved AI policy for one running service."""

    phi_egress_enabled: bool
    baa_reference: str
    environment: str

    @property
    def allows_phi_egress(self) -> bool:
        """Both conditions, always. Never one or the other."""
        return bool(self.phi_egress_enabled) and bool((self.baa_reference or "").strip())

    @staticmethod
    def from_settings(settings) -> "AIPolicy":
        return AIPolicy(
            phi_egress_enabled=bool(getattr(settings, "ai_phi_egress_enabled",
                                            PHI_AI_EGRESS_DEFAULT)),
            baa_reference=getattr(settings, "ai_phi_baa_reference", "") or "",
            environment=settings.environment.value,
        )

    @staticmethod
    def closed(environment: str = "unknown") -> "AIPolicy":
        """The policy that applies when configuration is absent: deny."""
        return AIPolicy(phi_egress_enabled=PHI_AI_EGRESS_DEFAULT,
                        baa_reference="", environment=environment)


def evaluate_phi_ai_egress(policy: Optional[AIPolicy]) -> None:
    """Raise unless PHI-bearing AI egress is explicitly permitted.

    Takes no content parameter, by design. A function that accepted the payload
    would invite a caller to log or forward it on the deny path; this one
    cannot, because it never receives it.
    """
    if policy is None:
        raise AIEgressDenied("no AI policy configured; PHI egress denied by default")
    if not policy.phi_egress_enabled:
        raise AIEgressDenied("PHI-bearing AI egress is disabled for this environment")
    if not (policy.baa_reference or "").strip():
        raise AIEgressDenied("PHI-bearing AI egress requires a named BAA reference")
