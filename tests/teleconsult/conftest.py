"""Run historical migration DDL with real commits, without altering other suites."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from config.settings.database import postgres_options
from django.db import connections
from django.db.backends.postgresql.base import DatabaseWrapper
from psycopg import sql

from isolated_pg_fixtures import set_cluster_environment
from rbac_fixtures import RbacGraph
from scheduling.world_database import clone_database

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from ops.testing.renewal_runner import ProvisionedDatabase


@contextmanager
def _committed_world(
    request: pytest.FixtureRequest, db: ProvisionedDatabase
) -> Iterator[RbacGraph]:
    name = "teleconsult_rehearsal_" + uuid4().hex
    passwords = {
        "clinic_owner": db.owner_password,
        "clinic_app": db.app_password,
        "clinic_agent": "",
    }
    with pytest.MonkeyPatch.context() as patch:
        set_cluster_environment(db, patch)
        # Clone an idle, unsealed, fully migrated database with its actual ACLs.
        clone_database(name, db.database)
        try:
            with ExitStack() as restore:
                for alias in ("default", "locks", "agent"):
                    old = connections[alias]
                    config = deepcopy(old.settings_dict)
                    config.update(
                        HOST="127.0.0.1",
                        PORT=str(db.port),
                        NAME=name,
                        PASSWORD=passwords[config["USER"]],
                        OPTIONS=postgres_options(),
                    )
                    patch.setitem(connections.settings, alias, config)
                    connections[alias] = DatabaseWrapper(config, alias=alias)
                    restore.callback(connections.__setitem__, alias, old)
                    restore.callback(connections[alias].close)
                graph = request.getfixturevalue("rbac_graph")
                assert isinstance(graph, RbacGraph)
                request.getfixturevalue("synthetic_provider")
                yield graph
        finally:
            # No FORCE: a leaked session fails the fixture instead of being hidden.
            with psycopg.connect(db.super_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture
def committed_migration_world(
    request: pytest.FixtureRequest, migrated_pg_cluster: ProvisionedDatabase
) -> AbstractContextManager[RbacGraph]:
    return _committed_world(request, migrated_pg_cluster)
