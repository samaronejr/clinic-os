"""Verify the approved forward plan at its two logical schema boundaries."""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Final, cast

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
    from django.db.migrations.executor import MigrationExecutor
    from psycopg import Connection

    from apps.tenancy.migrations._resumable_schema import Cut, MigrationKey

REFERENCE: Final = Path(__file__).with_name("migration_plan_pg16.json")
POSTGRES_MAJOR: Final = 16
_METADATA = re.compile(
    rb"(?m)^(?:\\(?:un)?restrict [A-Za-z0-9]+|-- Dumped (?:from|by) .*)\n"
)
_OBJECT = re.compile(rb"(?m)^--\n-- Name: [^\n]+\n--\n")
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
        dump[start:stop].rstrip()
        for start, stop in zip(starts, [*starts[1:], end], strict=True)
    ]
    return b"\n".join([dump[: starts[0]].rstrip(), *sorted(bodies)])


def schema_digest(db: BaseDatabaseWrapper) -> str:
    """Hash one schema-only dump plus the extension identities it abbreviates."""
    db.ensure_connection()
    conn = cast("Connection[tuple[object, ...]]", db.connection)
    info = conn.info
    if info.server_version // 10000 != POSTGRES_MAJOR:
        refuse("the approved schema projection requires PostgreSQL 16")
    executable = which("pg_dump")
    if executable is None:
        refuse("the approved plan requires a PostgreSQL 16 pg_dump client")
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
        # pg_dump emits CREATE EXTENSION, not member DDL. Record generic
        # extension membership by logical object identity, not catalog OIDs.
        cursor.execute("SET LOCAL search_path = pg_catalog")
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
    return sha256(payload).hexdigest()


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


def _check(db: BaseDatabaseWrapper, expected: str, *, edge: str) -> None:
    if schema_digest(db) != expected:
        refuse(
            f"{edge} logical schema differs from the approved pg_dump projection "
            "(unexpected DDL, including operator-created indexes, is unsupported)"
        )


def _reference() -> Mapping[str, str]:
    try:
        return cast("dict[str, str]", json.loads(REFERENCE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        refuse("missing or unreadable approved PostgreSQL 16 plan reference")


def verify_start(executor: MigrationExecutor) -> tuple[str, Cut | None]:
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
    current = reference.get(
        boundary(applied, interrupted, recorded=recorder.has_table())
    )
    destination = reference.get(boundary(loader.graph.nodes, None, recorded=True))
    if current is None or destination is None:
        refuse("Django ledger and progress do not identify an approved plan boundary")
    _check(db, current, edge="entry")
    return destination, interrupted


def verify_end(db: BaseDatabaseWrapper, expected: str) -> None:
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
