"""A migrated PG16 test lease independent of the shared Django test cluster."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from ops.testing import renewal_runner
from ops.testing.runtime_paths import runtime_directory

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="module")
def migrated_pg_cluster(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[renewal_runner.ProvisionedDatabase]:
    """Each consuming module starts with its own freshly migrated cluster."""
    with migrated_lease(tmp_path_factory.getbasetemp()) as db:
        yield db


@contextmanager
def migrated_lease(root: Path) -> Iterator[renewal_runner.ProvisionedDatabase]:
    repo = Path(__file__).resolve().parents[1]
    with (
        runtime_directory(root, purpose="posture-migrations") as logs,
        renewal_runner._provision_database(repo, f"posture_{uuid4().hex}") as db,
    ):
        renewal_runner._create_test_database(repo, db)
        renewal_runner._migrate(repo, db.owner_dsn, logs / "primary.log")
        renewal_runner._migrate(
            repo,
            database_url_for_name(db.owner_dsn, f"test_{db.database}"),
            logs / "test.log",
        )
        yield db


@pytest.fixture
def clean_posture_environment(
    migrated_pg_cluster: renewal_runner.ProvisionedDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_cluster_environment(migrated_pg_cluster, monkeypatch)


def set_cluster_environment(
    db: renewal_runner.ProvisionedDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in (
        ("APP_DATABASE_URL", db.app_dsn),
        ("MIGRATION_DATABASE_URL", db.owner_dsn),
        ("TEST_SUPERUSER_DATABASE_URL", db.super_dsn),
        ("TEST_DATABASE_NAME", f"test_{db.database}"),
        ("CLINIC_POSTURE_PASSWORD", db.posture_password),
    ):
        monkeypatch.setenv(name, value)
