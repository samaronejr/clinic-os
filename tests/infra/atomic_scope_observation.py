"""Observe the F6 publication laws on a quiescent predecessor clone."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.tenancy.migration_preflight import (
    _METADATA,
    _OBJECT,
    _dump_schema,
    _policy_roles,
)
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from psycopg import sql

from database_urls import database_url_for_name
from infra.atomic_scope_data import committed_data, recorded_row

if TYPE_CHECKING:
    import pytest
    from apps.tenancy.migrations._resumable_schema import MigrationKey
    from django.db.backends.postgresql.base import DatabaseWrapper

    from infra.atomic_scope_data import RecorderRow, Relation

type Address = tuple[int, int]
type ACL = dict[Address, tuple[str, ...]]
type Comments = dict[tuple[int, int, int], str]
type Names = frozenset[tuple[str, str]]

_RELATIONS = """
SELECT c.oid, n.nspname, c.relname, c.relkind,
       COALESCE(i.indrelid, owned.refobjid)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_index i ON i.indexrelid = c.oid
LEFT JOIN pg_depend owned ON c.relkind = 'S'
    AND owned.classid = 'pg_class'::regclass AND owned.objid = c.oid
    AND owned.refclassid = 'pg_class'::regclass
    AND owned.refobjsubid > 0 AND owned.deptype IN ('a', 'i')
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_'
"""
_ACL_COLUMNS = """
SELECT c.oid, c.relname, a.attname
FROM pg_class c
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE c.relnamespace = 'pg_catalog'::regnamespace
  AND c.relkind = 'r' AND NOT c.relisshared
  AND a.atttypid = 'aclitem[]'::regtype
  AND EXISTS (SELECT FROM pg_attribute k
              WHERE k.attrelid = c.oid AND k.attname = 'oid' AND NOT k.attisdropped)
"""
_COMMENTABLE = """
SELECT classid, objid FROM pg_depend
UNION SELECT refclassid, refobjid FROM pg_depend
UNION SELECT classoid, objoid FROM pg_description
"""
_ALLOWED_LOCKS = ("AccessShareLock", "RowShareLock", "ShareRowExclusiveLock")
# pg_dump emits these session settings at the *end* of whichever block
# precedes the first relevant table. Never remove settings inside an object.
_FLOATING_SET = re.compile(
    rb"\n\n(?:SET default_(?:table_access_method|tablespace) = [^\n]+;\n\n?)+\s*\Z"
)
_END = b"--\n-- PostgreSQL database dump complete\n--"


@dataclass(frozen=True)
class Publication:
    tables: Names
    helpers: Names = frozenset()


def _relations(db: DatabaseWrapper) -> dict[int, Relation]:
    with db.cursor() as cursor:
        cursor.execute(_RELATIONS)
        return {oid: (ns, name, kind, owner) for oid, ns, name, kind, owner in cursor}


@dataclass(frozen=True)
class Authority:
    acl: ACL
    comments: Comments
    commentable: frozenset[Address]


def _authority(db: DatabaseWrapper) -> Authority:
    acl: ACL = {}
    with db.cursor() as cursor:
        cursor.execute(_ACL_COLUMNS)
        for classid, catalog, column in cursor.fetchall():
            effective: sql.Composable = sql.Identifier(column)
            if catalog == "pg_proc":
                # A GRANT then REVOKE can materialize the default routine ACL.
                effective = sql.SQL("COALESCE({}, acldefault('f', proowner))").format(
                    effective
                )
            cursor.execute(
                sql.SQL(
                    "SELECT oid, ARRAY(SELECT a::text FROM unnest({}) a ORDER BY 1) "
                    "FROM pg_catalog.{}"
                ).format(effective, sql.Identifier(catalog))
            )
            acl.update({(classid, oid): tuple(grants) for oid, grants in cursor})
        cursor.execute(_COMMENTABLE)
        commentable = frozenset(cursor.fetchall()) | acl.keys()
        cursor.execute(
            "SELECT classoid, objoid, objsubid, description FROM pg_description"
        )
        comments = {(cls, oid, sub): text for cls, oid, sub, text in cursor}
    return Authority(acl, comments, commentable)


def _blocks(dump: bytes) -> Counter[bytes]:
    dump = _METADATA.sub(b"", dump)
    end = dump.rfind(_END)
    assert end >= 0, "missing pg_dump footer"
    starts = [match.start() for match in _OBJECT.finditer(dump[:end])]
    return Counter(
        _policy_roles(_FLOATING_SET.sub(b"", dump[start:stop]).rstrip())
        for start, stop in zip(starts, [*starts[1:], end], strict=False)
    )


def _helper_block(block: bytes, helpers: Names) -> bool:
    header = block.splitlines()[1].removeprefix(b"-- Name: ")
    name, separator, tail = header.partition(b"; Type: FUNCTION; Schema: ")
    if not separator:
        return False
    namespace = tail.split(b"; Owner: ", 1)[0]
    return (namespace.decode(), name.decode()) in helpers


def _new_relations_match(
    old: dict[int, Relation], current: dict[int, Relation], publication: Publication
) -> bool:
    added = {oid: row for oid, row in current.items() if oid not in old}
    tables = {oid for oid, (_, _, kind, _) in added.items() if kind == "r"}
    return (
        all(oid in current and current[oid] == row for oid, row in old.items())
        and bool(tables)
        and {(added[oid][0], added[oid][1]) for oid in tables} == publication.tables
        and all(
            oid in tables or (kind in {"i", "S"} and owner in tables)
            for oid, (_, _, kind, owner) in added.items()
        )
    )


def _apply_and_observe_locks(
    db: DatabaseWrapper,
    key: MigrationKey,
    old: dict[int, Relation],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RecorderRow, set[str]]:
    executor = MigrationExecutor(db)
    [(actual, backwards)] = executor.migration_plan([key])
    assert not backwards
    assert (actual.app_label, actual.name) == key
    record = MigrationRecorder.record_applied
    violations: set[str] = set()
    observed = False
    entry: RecorderRow | None = None

    def before_record(recorder: MigrationRecorder, app: str, name: str) -> None:
        nonlocal entry, observed
        assert (app, name) == key
        assert db.in_atomic_block
        assert not observed
        observed = True
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT relation, mode FROM pg_locks "
                "WHERE pid = pg_backend_pid() AND locktype = 'relation' AND granted"
            )
            if any(oid in old and mode not in _ALLOWED_LOCKS for oid, mode in cursor):
                violations.add("L3")
        record(recorder, app, name)
        entry = recorded_row(db, app, name)

    applied = set(executor.recorder.applied_migrations())
    with monkeypatch.context() as patch:
        patch.setattr(MigrationRecorder, "record_applied", before_record)
        executor.migrate([key])
    assert observed
    assert not db.in_atomic_block
    assert set(executor.recorder.applied_migrations()) == applied | {key}
    assert entry is not None
    return entry, violations


def laws_broken(
    db: DatabaseWrapper,
    key: MigrationKey,
    publication: Publication,
    monkeypatch: pytest.MonkeyPatch,
    *,
    admin_url: str,
) -> set[str]:
    """Commit one real migration and compare its before and after effects."""
    old = _relations(db)
    observer_url = database_url_for_name(admin_url, str(db.settings_dict["NAME"]))
    before_data = committed_data(observer_url, old)
    includes = [
        f"--table={sql.Identifier(ns, name).as_string()}"
        for ns, name, kind, _ in old.values()
        if kind in {"r", "p", "f", "m", "v", "S"}
    ]
    assert includes
    relations = _blocks(_dump_schema(db, "--strict-names", *includes))
    other_selection = ("--exclude-table=*.*", "--no-acl", "--no-comments")
    other = _blocks(_dump_schema(db, *other_selection))
    authority = _authority(db)
    entry, violations = _apply_and_observe_locks(db, key, old, monkeypatch)
    current = _relations(db)
    surviving_old = {
        oid: relation for oid, relation in old.items() if current.get(oid) == relation
    }
    after_data = committed_data(observer_url, surviving_old, entry)
    if not after_data.preserves(before_data, entry):
        violations.add("L5")
    if not _new_relations_match(old, current, publication) or relations != _blocks(
        _dump_schema(db, "--strict-names", *includes)
    ):
        violations.add("L1")
    after_other = _blocks(_dump_schema(db, *other_selection))
    if other - after_other or not all(
        _helper_block(block, publication.helpers) for block in after_other - other
    ):
        violations.add("L2")
    after = _authority(db)
    old_acl = {
        addr: after.acl[addr] for addr in authority.acl.keys() & after.acl.keys()
    }
    old_comments = {
        addr: value
        for addr, value in after.comments.items()
        if addr[:2] in authority.commentable
    }
    if authority.acl != old_acl or authority.comments != old_comments:
        violations.add("L4")
    return violations
