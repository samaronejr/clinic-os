"""Prescription tests use the same reference seed as migrated databases."""

import pytest

from prescription.policy_support import seed_class_policies


@pytest.fixture(autouse=True)
def class_policies(db: None) -> None:
    """Restore migration data lost by the previous transactional test's flush."""
    seed_class_policies()
