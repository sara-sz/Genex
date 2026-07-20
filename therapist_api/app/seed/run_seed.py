"""Seed utility entry point (dev-only, fictional data).

In this phase it does NOT connect to Firestore — it loads fixtures into an
in-memory repository to prove the flow and the dev-only guard. It refuses to run
outside dev.

Usage (local, dev only):
    ENVIRONMENT=dev GCP_PROJECT_ID=genex-provider-dev-2026 \
    FIREBASE_PROJECT_ID=genex-provider-dev-2026 \
    FIRESTORE_PROJECT_ID=genex-provider-dev-2026 REGION=us-central1 \
    python -m app.seed.run_seed
"""

from __future__ import annotations

from ..repository.memory import InMemoryRepository
from ..settings import Settings
from . import fixtures
from .guard import assert_dev


def seed(repo: InMemoryRepository) -> dict:
    """Load fictional fixtures into the given repo. Returns a small summary."""
    repo.set("therapist_profiles", "ther_hannah", fixtures.hannah_slp().model_dump())
    repo.set("child_references", "child_a", fixtures.child_a().model_dump())
    repo.set(
        "connections",
        "conn_hannah_childa",
        fixtures.hannah_child_a_connection().model_dump(),
    )
    return {
        "therapists": 1,
        "children": 1,
        "connections": 1,
        "plan_items": len(fixtures.child_a_weekly_plan()),
    }


def main() -> None:
    settings = Settings.from_env()
    assert_dev(settings)  # refuses outside dev
    repo = InMemoryRepository()
    summary = seed(repo)
    print(f"[seed] dev fixtures loaded (in-memory, no Firestore): {summary}")


if __name__ == "__main__":
    main()
