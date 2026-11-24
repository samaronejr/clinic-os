"""Independent schema oracle and real PostgreSQL mutations for recovery tests."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from hashlib import sha256
from typing import TYPE_CHECKING

import psycopg
from ops.testing.restore_container import ContainerPostgres
from psycopg import sql

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from django.db.backends.postgresql.base import DatabaseWrapper


def schema_hash(db: DatabaseWrapper) -> str:
    cfg = db.settings_dict
    args = (
        "--schema-only",
        "--schema=clinic_app",
        "--no-password",
        "--username=clinic_owner",
        f"--dbname={cfg['NAME']}",
    )
    if client := shutil.which("pg_dump"):
        # Fixed read-only operation; no password in the argv, inherited
        # environment or error message. Use the connection's actual endpoint.
        raw = subprocess.check_output(  # noqa: S603
            [client, *args, f"--host={cfg['HOST']}", f"--port={cfg['PORT']}"],
            env={"PGPASSWORD": cfg["PASSWORD"], "LC_ALL": "C"},
            timeout=120,
        )
    else:
        container = ContainerPostgres(
            os.environ["POSTGRES_CONTAINER"],
            cfg["NAME"],
            cfg["PASSWORD"],
            role="clinic_owner",
        )
        raw = container.execute(
            (
                "pg_dump",
                *args,
                "--host=127.0.0.1",
                "--port=" + os.environ["POSTGRES_PORT"],
            )
        )
    # PG16's session-random psql guard is not part of the schema.
    return sha256(
        b"\n".join(
            line
            for line in raw.splitlines()
            if not line.startswith((b"\\restrict ", b"\\unrestrict "))
        )
    ).hexdigest()


def heaps(db: DatabaseWrapper, scope: list[str]) -> list[tuple[str, str | None]]:
    """The live cut determines which written heaps and clustering indexes exist."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, (
                SELECT idx.relname FROM pg_index i
                JOIN pg_class idx ON idx.oid=i.indexrelid
                WHERE i.indrelid=c.oid AND i.indisvalid AND i.indisready AND i.indislive
                  AND i.indpred IS NULL AND pg_index_has_property(idx.oid,'clusterable')
                ORDER BY i.indisclustered DESC, i.indisprimary DESC,
                         idx.relname COLLATE "C"
                LIMIT 1)
            FROM pg_class c WHERE c.relnamespace='clinic_app'::regnamespace
              AND c.relkind='r' AND c.relname=ANY(%s)
            ORDER BY c.relname COLLATE "C"
        """,
            [scope],
        )
        return list(cur.fetchall())


def maintain(db: DatabaseWrapper, scope: list[str], operation: str) -> list[str]:
    statements = {
        "ANALYZE": sql.SQL("ANALYZE {}"),
        "REINDEX": sql.SQL("REINDEX TABLE {}"),
        "VACUUM": sql.SQL("VACUUM FULL {}"),
    }
    targets = heaps(db, scope)
    assert targets, f"No live heaps at the cut in {scope}"
    applied = []
    with db.cursor() as cur:
        for name, idx in targets:
            table = sql.Identifier("clinic_app", name)
            if operation == "CLUSTER":
                if idx is None:  # No eligible index; do not invent one.
                    continue
                cur.execute(
                    sql.SQL("CLUSTER {} USING {}").format(table, sql.Identifier(idx))
                )
            else:
                cur.execute(statements[operation].format(table))
            applied.append(name)
    return applied


def cluster_state(
    db: DatabaseWrapper, names: list[str]
) -> dict[str, tuple[int, str | None]]:
    """Heap file and persistent index designation, independent of the verifier."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, pg_relation_filenode(c.oid), idx.relname
            FROM pg_class c
            LEFT JOIN pg_index i ON i.indrelid=c.oid AND i.indisclustered
            LEFT JOIN pg_class idx ON idx.oid=i.indexrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
            """,
            [names],
        )
        return {name: (file, index) for name, file, index in cur.fetchall()}


def restore_designation(db: DatabaseWrapper, table: str, index: str | None) -> None:
    with db.cursor() as cur:
        designation = (
            sql.SQL("SET WITHOUT CLUSTER")
            if index is None
            else sql.SQL("CLUSTER ON {}").format(sql.Identifier(index))
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} {}").format(
                sql.Identifier("clinic_app", table), designation
            )
        )


def _change_default(db: DatabaseWrapper, names: list[str]) -> str:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,a.attname,a.atthasdef FROM pg_attribute a
            JOIN pg_class c ON c.oid=a.attrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
              AND a.attnum>0 AND NOT a.attisdropped
              AND (a.atthasdef OR a.atttypid='uuid'::regtype)
              AND a.attidentity='' AND a.attgenerated=''
            ORDER BY a.atthasdef DESC,c.relname COLLATE "C",a.attnum LIMIT 1
        """,
            [names],
        )
        row = cur.fetchone()
        assert row, f"No mutable default in {names}"
        table, col, present = row
        change = (
            sql.SQL("DROP DEFAULT")
            if present
            else sql.SQL("SET DEFAULT gen_random_uuid()")
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} ALTER {} {}").format(
                sql.Identifier("clinic_app", table), sql.Identifier(col), change
            )
        )
        return f"{table}.{col}"


def _change_trigger(db: DatabaseWrapper, names: list[str]) -> str:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,t.tgname,t.tgenabled FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
            ORDER BY t.tgisinternal,c.relname COLLATE "C",t.tgname COLLATE "C" LIMIT 1
        """,
            [names],
        )
        row = cur.fetchone()
        assert row, f"No trigger in {names}"
        table, name, enabled = row
        assert isinstance(table, str)
    # A scratch administrator can change an internal RI trigger. Retry always
    # reconnects as the ordinary migration owner, never the administrator.
    admin = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], db.settings_dict["NAME"]
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER TABLE {} {} TRIGGER {}").format(
                sql.Identifier("clinic_app", table),
                sql.SQL("ENABLE") if enabled == "D" else sql.SQL("DISABLE"),
                sql.Identifier(name),
            )
        )
    return table


def _invalidate_index(db: DatabaseWrapper, names: list[str]) -> str:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,ix.relname,pg_get_indexdef(i.indexrelid)
            FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
            JOIN pg_class ix ON ix.oid=i.indexrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
              AND c.relkind='r' AND ix.relkind='i' AND i.indisvalid AND i.indisready
              AND i.indislive AND NOT i.indisreplident AND NOT i.indisclustered
              AND NOT EXISTS (SELECT FROM pg_constraint WHERE conindid=i.indexrelid)
              AND NOT EXISTS (SELECT FROM pg_inherits WHERE inhrelid=ix.oid)
            ORDER BY c.relname COLLATE "C",ix.relname COLLATE "C" LIMIT 1
        """,
            [names],
        )
        row = cur.fetchone()
        assert row, f"No standalone index in {names}"
        table, index, definition = row
        cur.execute(
            sql.SQL("DROP INDEX {}").format(sql.Identifier("clinic_app", index))
        )
    admin = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], db.settings_dict["NAME"]
    )
    concurrent, changed = re.subn(
        r"\ACREATE (UNIQUE )?INDEX ",
        r"CREATE \1INDEX CONCURRENTLY ",
        definition,
        count=1,
    )
    assert changed == 1
    # CREATE INDEX CONCURRENTLY first commits the invalid catalog stub and
    # then waits for writers. A held writer lock guarantees the timeout, not
    # a race or a direct UPDATE of pg_index.
    with (
        psycopg.connect(admin) as writer,
        psycopg.connect(admin, autocommit=True) as builder,
    ):
        writer.execute(
            sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE").format(
                sql.Identifier("clinic_app", table)
            )
        )
        builder.execute("SET ROLE clinic_owner")
        builder.execute("SET search_path=clinic_app,public")
        builder.execute("SET lock_timeout='250ms'")
        try:
            builder.execute(sql.SQL(concurrent))
        except psycopg.errors.LockNotAvailable:
            pass
        else:
            msg = "Concurrent index build did not stop behind its writer"
            raise AssertionError(msg)
    with db.cursor() as cur:
        cur.execute(
            "SELECT indisvalid FROM pg_index WHERE indexrelid=%s::regclass",
            [sql.Identifier("clinic_app", index).as_string()],
        )
        assert cur.fetchone() == (False,)
    return f"{table}.{index}"


def _plant_side_value(db: DatabaseWrapper, names: list[str]) -> str:
    # Fault injection, not a maintenance or runtime privilege: only touch a
    # disposable mutant clone. PostgreSQL builds the typed datum via real DDL.
    # Copying only the missing-value pair avoids also changing slots, grants,
    # foreign keys, or defaults, including on tightly constrained tables.
    database = db.settings_dict["NAME"]
    assert re.fullmatch(r"mig_resume_[0-9a-f]{12}", database)
    admin = database_url_for_name(os.environ["TEST_SUPERUSER_DATABASE_URL"], database)
    with psycopg.connect(admin) as conn:
        row = conn.execute(
            """
            SELECT c.relname, a.attname, a.attrelid, a.attnum,
                   a.atttypid::regtype::text, array_send(a.attmissingval)
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            WHERE c.relnamespace='clinic_app'::regnamespace AND c.relname=ANY(%s)
              AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
              AND a.atttypid IN ('uuid'::regtype, 'bigint'::regtype, 'integer'::regtype)
            ORDER BY (a.atttypid='uuid'::regtype) DESC, a.atthasmissing DESC,
                     c.relname COLLATE "C", a.attnum LIMIT 1
            """,
            [names],
        ).fetchone()
        assert row, f"No comparable column in {names}"
        name, col, relid, slot, kind, old = row
        donors = {
            "uuid": "23e579b3-54ff-4cf7-a75a-8d124ee89d73",
            "bigint": "-2147483629",
            "integer": "-2147483629",
        }
        value = donors[kind]
        conn.execute("CREATE TEMP TABLE matrix_donor () ON COMMIT DROP")
        conn.execute(
            sql.SQL("ALTER TABLE pg_temp.matrix_donor ADD v {} DEFAULT {}").format(
                sql.SQL(kind),
                sql.Literal(value),
            )
        )
        readback = conn.execute(
            """
            UPDATE pg_catalog.pg_attribute AS target
               SET atthasmissing=source.atthasmissing,
                   attmissingval=source.attmissingval
              FROM pg_catalog.pg_attribute AS source
             WHERE source.attrelid='pg_temp.matrix_donor'::regclass
               AND source.attname='v'
               AND source.atthasmissing AND source.atttypid=target.atttypid
               AND source.atttypmod=target.atttypmod
               AND target.attrelid=%s AND target.attnum=%s
            RETURNING target.atthasmissing, array_send(target.attmissingval)
            """,
            (relid, slot),
        ).fetchone()
        assert readback is not None
        assert readback[0]
        assert readback[1] is not None
        assert readback[1] != old
    return f"{name}.{col}"


TAMPERS = {
    "side-value": _plant_side_value,
    "declared-default": _change_default,
    "trigger": _change_trigger,
    "invalid-index": _invalidate_index,
}
