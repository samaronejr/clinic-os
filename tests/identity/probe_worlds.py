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

Nothing can change the template after seeding: it is locked (no
connections allowed, CONNECT revoked from every role; a connection attempt
must fail). A clone must still equal the template as seeded, or the test
fails (stale template): its digest, class by class (every row and sequence,
and the catalog the verdicts depend on: function definitions and settings,
relation and column ACLs and row security, policies, column defaults,
constraints, triggers, rules and views, default ACLs, schemas, operators,
types, database and role settings, the runtime roles, extensions), and its
session environment (settings, roles, database ACL and settings, catalog
shape, session state) are compared with the ones recorded right after
seeding.
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
    from collections.abc import Iterator
    from pathlib import Path

    from rbac_fixtures import RbacGraph

# A test's source of fresh world clones (a WorldFactory).
type SeededWorld = WorldFactory

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


_USER: Final = sql.SQL(
    "n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'"
)


def _acl(column: str) -> sql.Composed:
    """An ACL as order-insensitive text (GRANT after REVOKE may reorder)."""
    return sql.SQL(
        "(SELECT pg_catalog.array_agg(a::text ORDER BY a::text) "
        "FROM pg_catalog.unnest({}) a)::text"
    ).format(sql.SQL(column))


def _query(text: str, **acls: str) -> sql.Composed:
    return sql.SQL(text).format(
        user=_USER, **{name: _acl(column) for name, column in acls.items()}
    )


# What a clone must repeat of the template, by class. ``None`` marks the data
# classes (every row of every table, every sequence's state); the rest are
# the catalog the certifier's verdicts depend on, in every non-system schema.
DIGEST_CLASSES: Final[dict[str, sql.Composed | None]] = {
    "rows": None,
    "sequences": None,
    "functions": _query(
        "SELECT n.nspname, p.proname, "
        "pg_catalog.pg_get_function_identity_arguments(p.oid), "
        "CASE WHEN p.prokind = 'a' THEN p.oid::pg_catalog.regprocedure::text "
        "ELSE pg_catalog.pg_get_functiondef(p.oid) END, p.proconfig::text, "
        "{acl_p_proacl}, pg_catalog.pg_get_userbyid(p.proowner), "
        "p.prosecdef::text, p.provolatile::text, p.proleakproof::text "
        "FROM pg_catalog.pg_proc p "
        "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
        "WHERE {user} ORDER BY 1, 2, 3",
        acl_p_proacl="p.proacl",
    ),
    "relations": _query(
        "SELECT n.nspname, c.relname, c.relkind::text, "
        "{acl_c_relacl}, c.relrowsecurity::text, "
        "c.relforcerowsecurity::text, pg_catalog.pg_get_userbyid(c.relowner), "
        "c.reloptions::text FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE {user} ORDER BY 1, 2",
        acl_c_relacl="c.relacl",
    ),
    "columns": _query(
        "SELECT n.nspname, c.relname, a.attname, "
        "pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull::text, "
        "a.attgenerated::text, a.attidentity::text, {acl_a_attacl} "
        "FROM pg_catalog.pg_attribute a "
        "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE {user} AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, 2, 3",
        acl_a_attacl="a.attacl",
    ),
    "policies": _query(
        "SELECT n.nspname, c.relname, p.polname, p.polcmd::text, "
        "p.polpermissive::text, (SELECT pg_catalog.array_agg("
        "  CASE r WHEN 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(r) END "
        "  ORDER BY 1) FROM pg_catalog.unnest(p.polroles) r)::text, "
        "pg_catalog.pg_get_expr(p.polqual, p.polrelid), "
        "pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) "
        "FROM pg_catalog.pg_policy p "
        "JOIN pg_catalog.pg_class c ON c.oid = p.polrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE {user} ORDER BY 1, 2, 3"
    ),
    "defaults": _query(
        "SELECT n.nspname, c.relname, a.attname, "
        "pg_catalog.pg_get_expr(d.adbin, d.adrelid) FROM pg_catalog.pg_attrdef d "
        "JOIN pg_catalog.pg_attribute a "
        "  ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
        "JOIN pg_catalog.pg_class c ON c.oid = d.adrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE {user} ORDER BY 1, 2, 3"
    ),
    "constraints": _query(
        "SELECT n.nspname, co.conrelid::pg_catalog.regclass::text, "
        "co.contypid::pg_catalog.regtype::text, co.conname, "
        "pg_catalog.pg_get_constraintdef(co.oid) FROM pg_catalog.pg_constraint co "
        "JOIN pg_catalog.pg_namespace n ON n.oid = co.connamespace "
        "WHERE {user} ORDER BY 1, 2, 3, 4"
    ),
    "triggers": _query(
        "SELECT n.nspname, c.relname, t.tgname, pg_catalog.pg_get_triggerdef(t.oid), "
        "t.tgenabled::text FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal AND {user} ORDER BY 1, 2, 3"
    ),
    "rules": _query(
        "SELECT n.nspname, c.relname, r.rulename, pg_catalog.pg_get_ruledef(r.oid), "
        "r.ev_enabled::text FROM pg_catalog.pg_rewrite r "
        "JOIN pg_catalog.pg_class c ON c.oid = r.ev_class "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE {user} ORDER BY 1, 2, 3"
    ),
    "default_acl": _query(
        "SELECT pg_catalog.pg_get_userbyid(d.defaclrole), "
        "COALESCE(n.nspname, ''), d.defaclobjtype::text, "
        "{acl_d_defaclacl} FROM pg_catalog.pg_default_acl d "
        "LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace "
        "ORDER BY 1, 2, 3",
        acl_d_defaclacl="d.defaclacl",
    ),
    "namespaces": _query(
        "SELECT n.nspname, pg_catalog.pg_get_userbyid(n.nspowner), "
        "{acl_n_nspacl} FROM pg_catalog.pg_namespace n "
        "WHERE {user} ORDER BY 1",
        acl_n_nspacl="n.nspacl",
    ),
    "operators": _query(
        "SELECT n.nspname, o.oprname, o.oprleft::pg_catalog.regtype::text, "
        "o.oprright::pg_catalog.regtype::text, o.oprcode::pg_catalog.regproc::text "
        "FROM pg_catalog.pg_operator o "
        "JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace "
        "WHERE {user} ORDER BY 1, 2, 3, 4"
    ),
    "types": _query(
        "SELECT n.nspname, t.typname, t.typtype::text, "
        "pg_catalog.format_type(t.typbasetype, t.typtypmod), t.typnotnull::text, "
        "t.typdefault FROM pg_catalog.pg_type t "
        "JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
        "WHERE {user} ORDER BY 1, 2"
    ),
    "settings": _query(
        "SELECT CASE s.setdatabase WHEN 0 THEN 'role' ELSE 'database' END, "
        "CASE s.setrole WHEN 0 THEN '' "
        "ELSE pg_catalog.pg_get_userbyid(s.setrole) END, s.setconfig::text "
        "FROM pg_catalog.pg_db_role_setting s WHERE s.setdatabase IN (0, "
        "(SELECT oid FROM pg_catalog.pg_database "
        " WHERE datname = pg_catalog.current_database())) ORDER BY 1, 2"
    ),
    "roles": _query(
        "SELECT r.rolname, r.rolsuper::text, r.rolinherit::text, "
        "r.rolcanlogin::text, r.rolbypassrls::text, r.rolcreatedb::text, "
        "r.rolcreaterole::text, (SELECT pg_catalog.array_agg("
        "  pg_catalog.pg_get_userbyid(m.roleid)::text ORDER BY 1) "
        "  FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid)::text "
        "FROM pg_catalog.pg_roles r WHERE r.rolname LIKE 'clinic\\_%' ORDER BY 1"
    ),
    "extensions": _query(
        "SELECT e.extname, e.extversion, n.nspname FROM pg_catalog.pg_extension e "
        "JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace ORDER BY 1"
    ),
}


def _data(admin: psycopg.Connection[tuple[object, ...]]) -> dict[str, list[str]]:
    lines: dict[str, list[str]] = {"rows": [], "sequences": []}
    relations = admin.execute(
        sql.SQL(
            "SELECT n.nspname, c.relname, c.relkind::text "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'S') AND {} ORDER BY 1, 2"
        ).format(_USER)
    ).fetchall()
    for schema, name, kind in relations:
        table = sql.Identifier(str(schema), str(name))
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
        lines["sequences" if kind == "S" else "rows"].append(
            f"{schema}.{name} {row[0]}"
        )
    return lines


def digest(database: str) -> dict[str, str]:
    """Per class in ``DIGEST_CLASSES``, a hash of what the database holds,
    read as the superuser (no row security)."""
    found: dict[str, str] = {}
    with psycopg.connect(_superuser_url(database), autocommit=True) as admin:
        data = _data(admin) if None in DIGEST_CLASSES.values() else {}
        for name, query in DIGEST_CLASSES.items():
            if query is None:
                lines = data[name]
            else:
                lines = [repr(row) for row in admin.execute(query).fetchall()]
            found[name] = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    return found


def _connect_grantees(name: str) -> tuple[str, ...]:
    with psycopg.connect(_superuser_url("postgres"), autocommit=True) as admin:
        rows = admin.execute(
            "SELECT DISTINCT CASE a.grantee WHEN 0 THEN 'PUBLIC' "
            "ELSE pg_catalog.pg_get_userbyid(a.grantee) END "
            "FROM pg_catalog.pg_database d, pg_catalog.aclexplode(d.datacl) a "
            "WHERE d.datname = %s AND a.privilege_type = 'CONNECT' ORDER BY 1",
            [name],
        ).fetchall()
    return tuple(str(grantee) for (grantee,) in rows)


def lock(name: str) -> tuple[str, ...]:
    """Make the template unreachable: no connections at all, and no role
    with CONNECT on it. Returns the grantees whose CONNECT was revoked (a
    clone gets them back). Fails unless a connection attempt is refused."""
    grantees = _connect_grantees(name)
    database = sql.Identifier(name)
    with psycopg.connect(_superuser_url("postgres"), autocommit=True) as admin:
        admin.execute(
            sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(database)
        )
        for grantee in grantees:
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    database, probe_shards._grantee(grantee)
                )
            )
    assert not _connect_grantees(name), "a role keeps CONNECT on the template"
    try:
        psycopg.connect(_superuser_url(name)).close()
    except psycopg.OperationalError as error:
        refused = str(error)
    else:
        refused = ""
    if "not currently accepting connections" not in refused:
        message = f"the template {name} still accepts connections"
        raise StaleWorldError(message)
    return grantees


def _reconnectable(clone: str, grantees: tuple[str, ...]) -> None:
    """Give a clone of the locked template the CONNECT grants it had."""
    with psycopg.connect(_superuser_url("postgres"), autocommit=True) as admin:
        for grantee in grantees:
            admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(clone), probe_shards._grantee(grantee)
                )
            )


@dataclass(frozen=True, slots=True)
class Template:
    """The session's seeded world: its database, the Python world as seeded
    (only ever handed out as deep copies), its KEK directory, and what a
    clone of it must show."""

    name: str
    world: exemption_probes.ProbeWorld
    secret_dir: Path
    environment: probe_shards.Environment
    digest: dict[str, str]
    # Roles whose CONNECT the lock revoked; each clone gets them back.
    grantees: tuple[str, ...]


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
    recorded = digest(name)
    grantees = lock(name)
    _TEMPLATE["session"] = Template(
        name, seeded, secret_dir, environment, recorded, grantees
    )
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
    """Refuse a clone that is not the template as it was seeded: every
    declared digest class, then the session environment."""
    found = digest(clone)
    changed = sorted(name for name in found if found[name] != held.digest.get(name))
    if changed:
        message = (
            f"stale template {held.name}: its content changed after seeding "
            f"({', '.join(changed)})"
        )
        raise StaleWorldError(message)
    environment = probe_shards.environment()
    if environment != held.environment:
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
        self.clones.append(clone)  # dropped at close whatever happens next
        probe_shards._clone(clone, held.name)
        _reconnectable(clone, held.grantees)
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

    def template(self) -> Template:
        """The session template, seeded if this is its first use; no clone."""
        return template(self.rbac_graph, self.secret_root)

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
