"""Inspect inert ownership metadata in a private rollback; never use the grant."""

from __future__ import annotations

from typing import TYPE_CHECKING, LiteralString, NamedTuple

import pytest
from ops.db import posture
from psycopg import sql

from infra.test_routine_policy import _catalog, _safe

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg


class _Object(NamedTuple):
    kind: LiteralString
    name: LiteralString
    inquiry: LiteralString
    privilege: str


def _owner_case(
    db: psycopg.Connection,
    obj: _Object,
    *,
    indirect: bool,
) -> None:
    owner = "r4f_unprivileged_leaf" if indirect else "clinic_app"
    original = db.info.user
    assert original is not None
    if indirect:
        db.execute("CREATE ROLE r4f_unprivileged_leaf NOLOGIN")
        db.execute("CREATE ROLE r4f_unprivileged_bridge NOLOGIN")
        db.execute(
            "GRANT r4f_unprivileged_leaf TO r4f_unprivileged_bridge "
            "WITH INHERIT FALSE, SET TRUE"
        )
    kind, name = sql.SQL(obj.kind), sql.SQL(obj.name)
    change = sql.SQL("ALTER {} {} OWNER TO {}")
    revoke = sql.SQL("REVOKE ALL ON {} {} FROM PUBLIC, {}")
    db.execute(revoke.format(kind, name, sql.Identifier(original)))
    assert _safe(db)
    db.execute(change.format(kind, name, sql.Identifier(owner)))
    db.execute(revoke.format(kind, name, sql.Identifier(owner)))
    # PostgreSQL distinguishes the two privilege inquiries even for the owner.
    check = sql.SQL("SELECT {0}(%s,%s,%s), {0}(%s,%s,%s)").format(sql.SQL(obj.inquiry))
    assert db.execute(
        check,
        (
            owner,
            obj.name,
            obj.privilege,
            owner,
            obj.name,
            obj.privilege + " WITH GRANT OPTION",
        ),
    ).fetchone() == (False, True)
    if indirect:
        assert _safe(db)
        db.execute(
            "GRANT r4f_unprivileged_bridge TO clinic_app WITH INHERIT FALSE, SET TRUE"
        )
    assert not _safe(db)
    if indirect:
        db.execute("REVOKE r4f_unprivileged_bridge FROM clinic_app")
    else:
        db.execute(change.format(kind, name, sql.Identifier(original)))
    assert _safe(db)


@pytest.mark.parametrize(
    ("create", "kind", "obj", "inquiry", "privilege"),
    [
        (
            "CREATE FUNCTION clinic_app.r4f_inert() RETURNS integer "
            "LANGUAGE sql RETURN 0",
            "FUNCTION",
            "clinic_app.r4f_inert()",
            "has_function_privilege",
            "EXECUTE",
        ),
        (
            "CREATE FUNCTION public.r4f_inert() RETURNS integer LANGUAGE sql RETURN 0",
            "FUNCTION",
            "public.r4f_inert()",
            "has_function_privilege",
            "EXECUTE",
        ),
        (
            "CREATE TABLE public.r4f_inert (n integer)",
            "TABLE",
            "public.r4f_inert",
            "has_table_privilege",
            "SELECT",
        ),
        (
            "CREATE SEQUENCE public.r4f_inert",
            "SEQUENCE",
            "public.r4f_inert",
            "has_sequence_privilege",
            "USAGE",
        ),
        (
            "CREATE SCHEMA r4f_inert",
            "SCHEMA",
            "r4f_inert",
            "has_schema_privilege",
            "USAGE",
        ),
        (
            "CREATE TYPE public.r4f_inert AS ENUM ('constant')",
            "TYPE",
            "public.r4f_inert",
            "has_type_privilege",
            "USAGE",
        ),
    ],
    ids=["routine", "other-namespace-routine", "table", "sequence", "schema", "type"],
)
def test_owner_without_ordinary_privilege(
    create: LiteralString,
    kind: LiteralString,
    obj: LiteralString,
    inquiry: LiteralString,
    privilege: str,
) -> None:
    with _catalog() as db:
        db.execute(create)
        _owner_case(db, _Object(kind, obj, inquiry, privilege), indirect=False)


def test_two_hop_owner_without_ordinary_execute() -> None:
    with _catalog() as db:
        db.execute(
            "CREATE FUNCTION clinic_app.r4f_inert() RETURNS integer "
            "LANGUAGE sql RETURN 0"
        )
        _owner_case(
            db,
            _Object(
                "FUNCTION",
                "clinic_app.r4f_inert()",
                "has_function_privilege",
                "EXECUTE",
            ),
            indirect=True,
        )


def test_empty_large_object_owner_without_ordinary_read() -> None:
    with _catalog() as db:
        # Only allocate an empty object; no import, export, payload or I/O.
        row = db.execute("SELECT pg_catalog.lo_create(0)").fetchone()
        assert row is not None
        oid = int(row[0])
        original = db.info.user
        assert original is not None
        owner_sql = sql.SQL("ALTER LARGE OBJECT {} OWNER TO {}")
        assert _safe(db)
        db.execute(owner_sql.format(sql.Literal(oid), sql.Identifier("clinic_app")))
        db.execute(
            sql.SQL("REVOKE ALL ON LARGE OBJECT {} FROM PUBLIC, clinic_app").format(
                sql.Literal(oid)
            )
        )
        # PG16 has no has_largeobject_privilege(). Observe owner and empty ACL
        # independently; do not open the large object to test ordinary access.
        assert db.execute(
            "SELECT lomowner='clinic_app'::regrole, cardinality(lomacl) "
            "FROM pg_largeobject_metadata WHERE oid=%s",
            (oid,),
        ).fetchone() == (True, 0)
        assert not _safe(db)
        db.execute(owner_sql.format(sql.Literal(oid), sql.Identifier(original)))
        assert _safe(db)


def test_only_exact_owner_signature_and_delegation_can_be_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Test-only policy, never learned by the gate or installed in the source tree.
    with _catalog() as db:
        db.execute(
            "CREATE FUNCTION clinic_app.r4f_inert() RETURNS int LANGUAGE sql RETURN 0"
        )
        db.execute("ALTER FUNCTION clinic_app.r4f_inert() OWNER TO clinic_app")
        db.execute(
            "REVOKE ALL ON FUNCTION clinic_app.r4f_inert() FROM PUBLIC, clinic_app"
        )
        assert not _safe(db)
        digest = db.execute(
            "SELECT encode(sha256(convert_to(pg_get_functiondef("
            "'clinic_app.r4f_inert()'::regprocedure),'UTF8')),'hex')"
        ).fetchone()
        assert digest is not None
        row = [
            "clinic_app.r4f_inert()",
            "",
            "clinic_app",
            str(digest[0]),
            "clinic_app",
            "-",
            "test/0001_initial",
            "test/0001_initial",
            "test/0001_initial",
            "inert parser fixture, not a deployed exception",
        ]
        path = tmp_path / "fixture.tsv"
        path.write_text("\t".join(row))
        with pytest.raises(ValueError, match="invalid"):
            posture._read_policy(path)
        row[5] = "clinic_app"
        path.write_text("\t".join(row))
        policy = posture._routine_exceptions() | posture._read_policy(path)
        monkeypatch.setattr(posture, "_routine_exceptions", lambda: policy)
        assert _safe(db)
        db.execute("ALTER FUNCTION clinic_app.r4f_inert() RENAME TO r4f_unapproved")
        assert not _safe(db)


def test_reachable_approved_owner_requires_delegation_for_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _catalog() as db:
        db.execute("CREATE ROLE r4f_reviewed_leaf NOLOGIN")
        db.execute(
            "CREATE FUNCTION clinic_app.r4f_inert() RETURNS int LANGUAGE sql RETURN 0"
        )
        db.execute("ALTER FUNCTION clinic_app.r4f_inert() OWNER TO r4f_reviewed_leaf")
        db.execute(
            "REVOKE ALL ON FUNCTION clinic_app.r4f_inert() "
            "FROM PUBLIC, r4f_reviewed_leaf"
        )
        row = db.execute(
            "SELECT encode(sha256(convert_to(pg_get_functiondef("
            "'clinic_app.r4f_inert()'::regprocedure),'UTF8')),'hex')"
        ).fetchone()
        assert row is not None
        fields = [
            "clinic_app.r4f_inert()",
            "",
            "r4f_reviewed_leaf",
            str(row[0]),
            "clinic_app,r4f_reviewed_leaf",
            "r4f_reviewed_leaf",
            "test/0001_initial",
            "test/0001_initial",
            "test/0001_initial",
            "uninstalled, test-only delegation exception",
        ]
        path = tmp_path / "fixture.tsv"
        path.write_text("\t".join(fields))
        original = posture._routine_exceptions()
        policy = original | posture._read_policy(path)
        monkeypatch.setattr(posture, "_routine_exceptions", lambda: policy)
        assert _safe(db)
        db.execute("GRANT r4f_reviewed_leaf TO clinic_app WITH INHERIT FALSE, SET TRUE")
        assert not _safe(db)
        fields[5] = "clinic_app,r4f_reviewed_leaf"
        path.write_text("\t".join(fields))
        policy = original | posture._read_policy(path)
        assert _safe(db)
