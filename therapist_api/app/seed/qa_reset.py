"""Destructive QA utility (dev-only).

Wipes the collaboration store for repeatable QA runs. Refuses to run outside dev
via `assert_dev`. In this phase it operates on an in-memory repo only.
"""

from __future__ import annotations

from ..repository.memory import InMemoryRepository
from ..settings import Settings
from .guard import assert_dev


def reset(repo: InMemoryRepository) -> None:
    """Clear all collections (destructive)."""
    repo._data.clear()  # noqa: SLF001 — deliberate destructive reset in a dev tool


def main() -> None:
    settings = Settings.from_env()
    assert_dev(settings)  # refuses outside dev
    repo = InMemoryRepository()
    reset(repo)
    print("[qa_reset] dev store reset (in-memory).")


if __name__ == "__main__":
    main()
