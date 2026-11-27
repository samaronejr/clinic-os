"""Verify the approved forward plan at its two logical schema boundaries."""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Final, Never, cast

from django.db import transaction
from django.db.migrations.recorder import MigrationRecorder

from apps.tenancy.migrations._resumable_schema import (
    GuardedMigration,
    prefix,
    progress,
    refuse,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.backends.utils import CursorWrapper
    from django.db.migrations.executor import MigrationExecutor
    from psycopg import Connection

    from apps.tenancy.migrations._resumable_schema import Cut, MigrationKey

type IndexFingerprint = tuple[str, str]


@dataclass(frozen=True)
class SchemaProjection:
    """The logical dump digest and its complete user-index census."""

    digest: str
    indexes: frozenset[IndexFingerprint]


@dataclass(frozen=True)
class PlanReference:
    """Rehearsed fingerprints for the two supported forward walks."""

    # Deduplicate index identities across all boundaries. The bitmask records
    # which fingerprints are present at an edge, not which changes to allow.
    boundaries: Mapping[str, tuple[str, str]]
    indexes: tuple[IndexFingerprint, ...]

    def at(self, boundary_key: str) -> SchemaProjection | None:
        """Resolve a ledger boundary and decode its expected index set."""
        entry = self.boundaries.get(boundary_key)
        if entry is None:
            return None
        digest, encoded_mask = entry
        mask = int(encoded_mask, 16)
        return SchemaProjection(
            digest,
            frozenset(
                entry for bit, entry in enumerate(self.indexes) if mask & (1 << bit)
            ),
        )


REFERENCE: Final = Path(__file__).with_name("migration_plan_pg16.json")
POSTGRES_MAJOR: Final = 16
MAX_REPORTED_INDEXES: Final = 8
_METADATA = re.compile(
    rb"(?m)^(?:\\(?:un)?restrict [A-Za-z0-9]+|-- Dumped (?:from|by) .*)\n"
)
_OBJECT = re.compile(rb"(?m)^--\n-- Name: [^\n]+\n--\n")
_IDENT = rb'(?:"(?:[^"\n]|"")*"|[a-z_][a-z_0-9$]*)'
_POLICY_ROLES = re.compile(
    rb"\A--\n-- Name: [^\n]+; Type: POLICY; Schema: [^\n]+\n--\n\n"
    rb"CREATE POLICY %s ON %s\.%s"
    rb"(?: AS RESTRICTIVE)?(?: FOR (?:SELECT|INSERT|UPDATE|DELETE|ALL))? TO "
    rb"(?P<roles>%s(?:, %s)*)(?= USING | WITH CHECK |;)"
    % (_IDENT, _IDENT, _IDENT, _IDENT, _IDENT)
)
_ROLE = re.compile(_IDENT)
_CONNECTION = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "user": "PGUSER",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "gssencmode": "PGGSSENCMODE",
}


def _policy_roles(block: bytes) -> bytes:
    # pg_dump gathers the role names without ORDER BY. Reordering a policy's
    # TO set is not DDL drift; any change to its members or predicates is.
    match = _POLICY_ROLES.match(block)
    if match is None:
        return block
    start, end = match.span("roles")
    roles = b", ".join(sorted(_ROLE.findall(match["roles"])))
    return block[:start] + roles + block[end:]


def _normalize(dump: bytes) -> bytes:
    # Sort complete pg_dump object blocks, not individual DDL lines. Bodies,
    # column order, owner changes, ACL statements, etc. remain in the digest.
    # PostgreSQL may emit independent objects in different orders.
    dump = _METADATA.sub(b"", dump)
    starts = [match.start() for match in _OBJECT.finditer(dump)]
    end = dump.rfind(b"--\n-- PostgreSQL database dump complete\n--")
    if end < 0:
        refuse("invalid pg_dump output")
    if not starts:
        return dump[:end].rstrip()
    bodies = [
        _policy_roles(dump[start:stop].rstrip())
        for start, stop in zip(starts, [*starts[1:], end], strict=True)
    ]
    return b"\n".join([dump[: starts[0]].rstrip(), *sorted(bodies)])


def _schema_mismatch(edge: str, indexes: set[str]) -> Never:
    hint = ""
    if indexes:
        names = ", ".join(
            name.encode("unicode_escape").decode("ascii")
            for name in sorted(indexes)[:MAX_REPORTED_INDEXES]
        )
        remaining = len(indexes) - MAX_REPORTED_INDEXES
        extra = f" and {remaining} more" if remaining > 0 else ""
        hint = f"; index drift: {names}{extra}"
    refuse(
        f"{edge} logical schema differs from the approved pg_dump projection "
        f"(unexpected DDL is unsupported){hint}"
    )


def _index_census(
    cursor: CursorWrapper,
) -> tuple[frozenset[IndexFingerprint], set[str]]:
    # Unlike pg_dump, the live catalog has invalid and not-ready indexes.
    # Keep stable logical definitions, not OIDs or transient physical layout.
    cursor.execute(
        """
        SELECT pg_catalog.quote_ident(n.nspname::text) || '.' ||
               pg_catalog.quote_ident(c.relname::text),
               pg_catalog.pg_get_indexdef(i.indexrelid),
               i.indisvalid AND i.indisready AND i.indislive
        FROM pg_catalog.pg_index i
        JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
        """
    )
    rows = cursor.fetchall()
    return (
        frozenset(
            (name, sha256(definition.encode()).hexdigest())
            for name, definition, _ in rows
        ),
        {name for name, _, usable in rows if not usable},
    )


def schema_projection(
    db: BaseDatabaseWrapper,
    *,
    expected: SchemaProjection | None = None,
    edge: str = "",
) -> SchemaProjection:
    """Read the index census and hash one dump from the same snapshot."""
    db.ensure_connection()
    conn = cast("Connection[tuple[object, ...]]", db.connection)
    info = conn.info
    if info.server_version // 10000 != POSTGRES_MAJOR:
        refuse("the approved schema projection requires PostgreSQL 16")
    parameters = info.get_parameters()
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/"),
        "LC_ALL": "C",
        "PGCONNECT_TIMEOUT": "3",
        "PGPASSWORD": info.password,
        "PGHOST": info.host,
        "PGPORT": str(info.port),
        "PGUSER": info.user,
        "PGDATABASE": info.dbname,
        **{
            variable: parameters[key]
            for key, variable in _CONNECTION.items()
            if key in parameters
        },
    }
    with transaction.atomic(using=db.alias), db.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor.execute("SELECT pg_catalog.pg_export_snapshot()")
        row = cursor.fetchone()
        if row is None:
            refuse("cannot open a schema snapshot")
        snapshot = row[0]
        cursor.execute("SET LOCAL search_path = pg_catalog")
        indexes, unusable = _index_census(cursor)
        if expected is not None:
            drift = unusable | {name for name, _ in indexes ^ expected.indexes}
            if drift:
                _schema_mismatch(edge, drift)
        executable = which("pg_dump")
        if executable is None:
            refuse("the approved plan requires a PostgreSQL 16 pg_dump client")
        # pg_dump emits CREATE EXTENSION, not member DDL. Record generic
        # extension membership by logical object identity, not catalog OIDs.
        cursor.execute(
            """
            SELECT e.extname, e.extversion, i.type, i.identity
            FROM pg_catalog.pg_extension e
            LEFT JOIN pg_catalog.pg_depend d ON d.refobjid = e.oid
                AND d.refclassid = 'pg_catalog.pg_extension'::regclass
                AND d.deptype = 'e'
            LEFT JOIN LATERAL
                pg_catalog.pg_identify_object(d.classid, d.objid, d.objsubid) i
                ON true
            """
        )
        members = sorted(cursor.fetchall())
        try:
            dump = subprocess.run(  # noqa: S603 - fixed executable and flags
                [
                    executable,
                    "--no-password",
                    "--schema-only",
                    "--quote-all-identifiers",
                    "--encoding=UTF8",
                    "--lock-wait-timeout=5s",
                    "--snapshot=" + snapshot,
                ],
                env=environment,
                capture_output=True,
                timeout=30,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            # Never relay subprocess diagnostics or connection parameters.
            refuse(
                "pg_dump schema snapshot failed "
                "(check the PostgreSQL 16 client and owner access)"
            )
    payload = _normalize(dump) + b"\0" + json.dumps(members).encode()
    return SchemaProjection(sha256(payload).hexdigest(), indexes)


def schema_digest(db: BaseDatabaseWrapper) -> str:
    """Return the authoritative full logical digest."""
    return schema_projection(db).digest


def boundary(
    applied: Iterable[MigrationKey],
    interrupted: Cut | None,
    *,
    recorded: bool,
) -> str:
    """Index trusted reference digests by an exact ledger state, not DDL."""
    return sha256(
        json.dumps([sorted(applied), interrupted, recorded]).encode()
    ).hexdigest()


def _check(db: BaseDatabaseWrapper, expected: SchemaProjection, *, edge: str) -> None:
    observed = schema_projection(db, expected=expected, edge=edge)
    if observed.digest != expected.digest:
        _schema_mismatch(edge, set())


def _reference() -> PlanReference:
    try:
        raw = json.loads(REFERENCE.read_text(encoding="utf-8"))
        return PlanReference(
            cast("dict[str, tuple[str, str]]", raw["boundaries"]),
            tuple(
                (name, digest)
                for name, digest in cast("list[tuple[str, str]]", raw["indexes"])
            ),
        )
    except (OSError, ValueError, KeyError, TypeError):
        refuse("missing or unreadable approved PostgreSQL 16 plan reference")


def verify_start(executor: MigrationExecutor) -> tuple[SchemaProjection, Cut | None]:
    """Require a single ledger-declared boundary and compare to that reference."""
    db = executor.connection
    loader = executor.loader
    loader.check_consistent_history(db)
    recorder = MigrationRecorder(db)
    applied = set(recorder.applied_migrations())
    steps = progress(db)
    guarded = {
        key: len(migration.operations)
        for key, migration in loader.disk_migrations.items()
        if isinstance(migration, GuardedMigration)
    }
    pending = {
        migration for migration, _ in executor.migration_plan(loader.graph.leaf_nodes())
    }
    first = next(
        (
            (migration.app_label, migration.name)
            for migration, _ in executor.migration_plan(
                loader.graph.leaf_nodes(), clean_start=True
            )
            if migration in pending
        ),
        None,
    )
    if steps.keys() - guarded.keys():
        refuse("progress refers to an unknown guarded migration")
    interrupted = None
    for key, length in guarded.items():
        count = prefix(key, steps.get(key, ()), length)
        if key in applied:
            if count != length:
                refuse(
                    f"{'.'.join(key)} is recorded by Django without complete progress"
                )
        elif count:
            if key != first:
                refuse(
                    f"{'.'.join(key)} has progress outside the first pending migration"
                )
            interrupted = key, count

    reference = _reference()
    current = reference.at(
        boundary(applied, interrupted, recorded=recorder.has_table())
    )
    destination = reference.at(boundary(loader.graph.nodes, None, recorded=True))
    if current is None or destination is None:
        refuse("Django ledger and progress do not identify an approved plan boundary")
    _check(db, current, edge="entry")
    return destination, interrupted


def verify_end(db: BaseDatabaseWrapper, expected: SchemaProjection) -> None:
    """Do not publish an unrecognized schema after the forward plan."""
    _check(db, expected, edge="exit")


@contextmanager
def migration_lock(db: BaseDatabaseWrapper) -> Iterator[None]:
    """Serialize supported invocations across their transaction boundaries."""
    if not db.get_autocommit():
        refuse("the migration plan requires an autocommit connection")
    with db.cursor() as cursor:
        cursor.execute("SELECT current_user, pg_try_advisory_lock(524286, 9)")
        status = cursor.fetchone()
    if status is None or status[1] is not True:
        refuse("another migration plan owns the database lock")
    try:
        if status[0] != "clinic_owner":
            refuse("the migration plan must connect as clinic_owner")
        yield
    finally:
        with db.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(524286, 9)")
