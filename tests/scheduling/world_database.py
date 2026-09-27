"""Owned PostgreSQL template/clone operations for scheduling test isolation."""

from __future__ import annotations

import hashlib
import json
import os

import psycopg
from psycopg import sql

from database_urls import database_url_for_name


def admin_url(database: str) -> str:
    return database_url_for_name(os.environ["TEST_SUPERUSER_DATABASE_URL"], database)


def clone_database(name: str, source: str) -> None:
    with psycopg.connect(admin_url("postgres"), autocommit=True) as admin:
        row = admin.execute(
            "SELECT pg_get_userbyid(datdba),oid FROM pg_database WHERE datname=%s",
            [source],
        ).fetchone()
        assert row is not None
        settings = admin.execute(
            "SELECT setrole,setconfig FROM pg_db_role_setting WHERE setdatabase=%s",
            [row[1]],
        ).fetchall()
        assert not settings, (
            "scheduling template refuses unhandled database-specific settings"
        )
        admin.execute(
            sql.SQL("CREATE DATABASE {} WITH TEMPLATE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(source), sql.Identifier(row[0])
            )
        )
        admin.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(name)
            )
        )
        grants = admin.execute(
            "SELECT a.grantee,pg_get_userbyid(a.grantee),"
            "a.privilege_type,a.is_grantable "
            "FROM pg_database d,aclexplode(d.datacl) a WHERE d.datname=%s "
            "AND a.grantee<>d.datdba ORDER BY 1,3",
            [source],
        ).fetchall()
        privileges = {
            "CONNECT": sql.SQL("CONNECT"),
            "CREATE": sql.SQL("CREATE"),
            "TEMPORARY": sql.SQL("TEMPORARY"),
        }
        for oid, role, privilege, grantable in grants:
            admin.execute(
                sql.SQL("GRANT {} ON DATABASE {} TO {}{}").format(
                    privileges[privilege],
                    sql.Identifier(name),
                    sql.Identifier(role) if oid else sql.SQL("PUBLIC"),
                    sql.SQL(" WITH GRANT OPTION" if grantable else ""),
                )
            )


def drop_database(name: str) -> None:
    assert name.startswith(("scheduling_template_", "scheduling_world_"))
    with psycopg.connect(admin_url("postgres"), autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )


def content_digest(database: str) -> str:
    """All non-system tables and sequences, in one batched data query."""
    with psycopg.connect(admin_url(database), autocommit=True) as admin:
        relations = admin.execute(
            "SELECT n.nspname,c.relname,c.relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind IN ('r','p','S') AND n.nspname<>'information_schema' "
            "AND n.nspname !~ '^pg_' ORDER BY 1,2"
        ).fetchall()
        queries = []
        for schema, name, kind in relations:
            template = (
                sql.SQL(
                    "SELECT {} AS name,last_value::text||':'||is_called::text "
                    "AS value FROM {}"
                )
                if kind == "S"
                else sql.SQL(
                    "SELECT {} AS name,encode(sha256(convert_to(COALESCE("
                    "string_agg(t::text,E'\\n' ORDER BY t::text),''),'UTF8')),"
                    "'hex') AS value FROM {} t"
                )
            )
            queries.append(
                template.format(
                    sql.Literal(f"{schema}.{name}"), sql.Identifier(schema, name)
                )
            )
        rows = admin.execute(sql.SQL(" UNION ALL ").join(queries)).fetchall()
        environment = admin.execute(
            "SELECT current_setting('server_version_num'), "
            "(SELECT pg_get_userbyid(datdba) FROM pg_database "
            "WHERE datname=current_database()), "
            "(SELECT jsonb_agg(ROW(a.grantee,a.privilege_type,a.is_grantable) "
            "ORDER BY a.grantee,a.privilege_type) FROM pg_database d,"
            "aclexplode(d.datacl) a WHERE d.datname=current_database()), "
            "(SELECT jsonb_agg(ROW(oid,rolname,rolsuper,rolinherit,rolcanlogin,"
            "rolbypassrls,rolconfig) ORDER BY oid) FROM pg_roles), "
            "(SELECT jsonb_agg(m ORDER BY roleid,member,grantor) "
            "FROM pg_auth_members m)"
        ).fetchone()
    return hashlib.sha256(
        json.dumps([rows, environment], sort_keys=True).encode()
    ).hexdigest()
