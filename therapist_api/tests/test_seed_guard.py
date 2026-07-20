"""Seed + destructive QA utilities refuse to run outside dev."""

from __future__ import annotations

import pytest

from app.repository.memory import InMemoryRepository
from app.seed.guard import SeedRefused, assert_dev
from app.seed.qa_reset import reset
from app.seed.run_seed import seed
from tests.conftest import dev_settings, prod_settings


def test_assert_dev_allows_dev():
    assert_dev(dev_settings())  # no raise


def test_seed_utility_refuses_prod():
    with pytest.raises(SeedRefused):
        assert_dev(prod_settings())


def test_destructive_qa_refuses_prod():
    # Simulate a QA tool guarding itself before wiping.
    with pytest.raises(SeedRefused):
        assert_dev(prod_settings())


def test_assert_dev_refuses_wrong_dev_project():
    with pytest.raises(SeedRefused):
        assert_dev(dev_settings(GCP_PROJECT_ID="some-other-project"))


def test_seed_loads_fixtures_in_memory():
    repo = InMemoryRepository()
    summary = seed(repo)
    assert summary["therapists"] == 1
    assert repo.exists("child_references", "child_a")


def test_qa_reset_clears_store():
    repo = InMemoryRepository()
    seed(repo)
    reset(repo)
    assert not repo.exists("child_references", "child_a")
