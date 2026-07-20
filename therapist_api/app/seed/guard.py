"""Dev-only guard for seed and destructive tooling (fail-closed).

Any utility that writes fixtures or destroys data must call `assert_dev` first.
It refuses to run unless the resolved environment is exactly 'dev' AND the target
project is the canonical dev project. Prod (or anything non-dev) raises.
"""

from __future__ import annotations

from ..constants import CANONICAL_PROJECT
from ..settings import Settings


class SeedRefused(RuntimeError):
    """Raised when seed/destructive tooling is invoked outside dev."""


def assert_dev(settings: Settings) -> None:
    if settings.environment != "dev":
        raise SeedRefused(
            f"Refusing to run: environment is '{settings.environment or '<unset>'}', "
            "not 'dev'. Seed/destructive tooling is dev-only."
        )
    expected = CANONICAL_PROJECT["dev"]
    if settings.gcp_project_id and settings.gcp_project_id != expected:
        raise SeedRefused(
            f"Refusing to run: GCP_PROJECT_ID '{settings.gcp_project_id}' is not the "
            f"canonical dev project '{expected}'."
        )
