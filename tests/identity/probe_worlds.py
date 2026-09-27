"""One seeded probe world per session, a fresh clone of it for every test.

Seeding a probe world (the legacy world, operational, prescription and
teleconsult subjects, the 1,414 matrix states' rows, the actor catalog) is
the same work for every certifier test. It runs once per session, into a
template database cloned from the test database at first use; every test
then gets:

- its own database, cloned from the template (``CREATE DATABASE ...
  TEMPLATE``, with the source's owner and ACL; identity/probe_shards.py), and
  its own connection to it; the test database itself is left alone;
- a deep copy of the seeded Python world, so no mutable state (requests,
  matrix bookkeeping, observer caches) is shared between tests;
- the per-test process state the seeding left behind: the teleconsult
  dispatch capture, the send adapter, function statistics on its connection,
  and the tenant KEK the world's keys are wrapped with (copied once into a
  session directory).

A clone must equal the template as seeded, or the test fails (stale
template): its content digest (every table's rows and every sequence, in
every non-system schema) and its session environment (settings, roles,
database ACL and settings, catalog shape, session state) are compared with
the ones recorded right after seeding.
"""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from apps.comms.tasks import execute_operation
from apps.core import integration
from apps.identity import stepup
from apps.teleconsult.adapters import SyntheticRoomAdapter
from django.conf import settings
from django.db import connection, connections
from django.db.backends.postgresql.base import DatabaseWrapper
from django.test import override_settings
from psycopg import sql

from auth.stepup_test_support import STEP_UP_NOW
from database_urls import database_url_for_name
from identity import actor_channels, exemption_probes, probe_shards
from identity import legacy_operational_boundaries as operational
from identity import legacy_prescription_boundaries as prescriptions
from identity import legacy_teleconsult_boundaries as teleconsult
from identity.legacy_parity_support import world

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from rbac_fixtures import RbacGraph

# A test's source of fresh world clones.
type SeededWorld = Callable[[], exemption_probes.ProbeWorld]

SYNTHETIC: Final = {
    "BILLING_SYNTHETIC_PIX": True,
    "PRESCRIPTION_SYNTHETIC_SIGNING": True,
    "PHYSICIAN_SYNTHETIC_REGISTRY": True,
    "TELECONSULT_SYNTHETIC_PROVIDER": True,
}
TEMPLATE_PREFIX: Final = "probe_template_"
CLONE_PREFIX: Final = "probe_world_"


class StaleWorldError(AssertionError):
    """A world clone that is not the template as it was seeded."""


def _superuser_url(database: str) -> str:
    return database_url_for_name(os.environ["TEST_SUPERUSER_DATABASE_URL"], database)


def enable_statistics() -> None:
    """Function statistics on the current connection (the observer's)."""
    actor_channels.enable_function_statistics(
        _superuser_url(str(connection.settings_dict["NAME"]))
    )


def seed(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> exemption_probes.ProbeWorld:
    """Seed a probe world into the current database (the uncached path)."""
    enable_statistics()
    subject = world(rbac_graph, "receptionist")
    op = operational.seed_operational(subject)
    rx = prescriptions.seed_prescription(subject)
    tc = teleconsult.seed_teleconsult(subject, op, monkeypatch)
    return exemption_probes.build_world(subject, op, rx, tc)


def digest(database: str) -> str:
    """Every row of every table and every sequence's state, in every
    non-system schema, read as the superuser (no row security)."""
    lines: list[str] = []
    with psycopg.connect(_superuser_url(database), autocommit=True) as admin:
        relations = admin.execute(
            "SELECT n.nspname, c.relname, c.relkind::text "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'S') AND n.nspname <> 'information_schema' "
            "AND n.nspname NOT LIKE 'pg\\_%' ORDER BY 1, 2"
        ).fetchall()
        for schema, name, kind in relations:
            table = sql.Identifier(schema, name)
            query = (
                sql.SQL("SELECT last_value::text || ':' || is_called::text FROM {}")
                if kind == "S"
                else sql.SQL(
                    "SELECT pg_catalog.count(*)::text || ':' || pg_catalog.md5("
                    "COALESCE(pg_catalog.string_agg(t::text, E'\\n' ORDER BY t::text),"
                    " '')) FROM {} t"
                )
            )
            row = admin.execute(query.format(table)).fetchone()
            assert row is not None
            lines.append(f"{schema}.{name} {kind} {row[0]}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Template:
    """The session's seeded world: its database, the Python world as seeded
    (only ever handed out as deep copies), its KEK directory, and what a
    clone of it must show."""

    name: str
    world: exemption_probes.ProbeWorld
    secret_dir: Path
    environment: probe_shards.Environment
    digest: str


_TEMPLATE: dict[str, Template] = {}


@contextmanager
def connected(database: str) -> Iterator[None]:
    """Point the default connection at ``database`` for the block."""
    original = connections["default"]
    options = original.settings_dict.copy()
    options["NAME"] = database
    connections.close_all()
    connections["default"] = DatabaseWrapper(options, alias="default")
    try:
        yield
    finally:
        connections.close_all()
        connections["default"] = original


def template(rbac_graph: RbacGraph, secret_root: Path) -> Template:
    """The session template, seeded on first use from the current test's
    database (its ``rbac_graph`` rows become the world's graph)."""
    if "session" in _TEMPLATE:
        return _TEMPLATE["session"]
    source = str(connection.settings_dict["NAME"])
    name = TEMPLATE_PREFIX + uuid4().hex
    secret_dir = secret_root / name
    assert settings.CLINIC_SECRET_DIR, "the synthetic secret backend is set"
    shutil.copytree(settings.CLINIC_SECRET_DIR, secret_dir)
    connections.close_all()
    probe_shards._clone(name, source)
    with (
        connected(name),
        override_settings(CLINIC_SECRET_DIR=str(secret_dir), **SYNTHETIC),
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)
        seeded = seed(rbac_graph, patch)
        environment = probe_shards.environment()
    _TEMPLATE["session"] = Template(name, seeded, secret_dir, environment, digest(name))
    return _TEMPLATE["session"]


def template_now() -> Template:
    """The session template (it must have been seeded)."""
    return _TEMPLATE["session"]


def drop_template() -> None:
    """Drop the session template (session teardown)."""
    held = _TEMPLATE.pop("session", None)
    if held is not None:
        probe_shards._drop(held.name)
        shutil.rmtree(held.secret_dir, ignore_errors=True)


def check_clone(clone: str, held: Template) -> None:
    """Refuse a clone that is not the template as it was seeded."""
    if digest(clone) != held.digest:
        message = f"stale template {held.name}: its content changed after seeding"
        raise StaleWorldError(message)
    found = probe_shards.environment()
    if found != held.environment:
        message = f"stale template {held.name}: its environment changed after seeding"
        raise StaleWorldError(message)


class WorldFactory:
    """Fresh world clones for one test; ``close`` restores the test's
    connection and drops them (idempotent)."""

    def __init__(
        self, rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, secret_root: Path
    ) -> None:
        self.rbac_graph = rbac_graph
        self.monkeypatch = monkeypatch
        self.secret_root = secret_root
        self.stack = ExitStack()
        self.clones: list[str] = []

    def __call__(self) -> exemption_probes.ProbeWorld:
        held = template(self.rbac_graph, self.secret_root)
        clone = CLONE_PREFIX + uuid4().hex
        connections.close_all()
        probe_shards._clone(clone, held.name)
        self.clones.append(clone)
        self.stack.enter_context(connected(clone))
        self.stack.enter_context(
            override_settings(CLINIC_SECRET_DIR=str(held.secret_dir))
        )
        dispatched: list[object] = []
        self.monkeypatch.setattr(
            execute_operation, "apply_async", lambda **kwargs: dispatched.append(kwargs)
        )
        integration.register_send_adapter(SyntheticRoomAdapter())
        enable_statistics()
        check_clone(clone, held)
        return copy.deepcopy(held.world)

    def close(self) -> None:
        self.stack.close()
        while self.clones:
            probe_shards._drop(self.clones.pop())


def leftovers() -> list[str]:
    """World clones still present (the session template is not one)."""
    with psycopg.connect(_superuser_url("postgres"), autocommit=True) as admin:
        rows = admin.execute(
            "SELECT datname FROM pg_catalog.pg_database WHERE datname LIKE %s "
            "ORDER BY 1",
            [CLONE_PREFIX + "%"],
        ).fetchall()
    return [str(name) for (name,) in rows]
