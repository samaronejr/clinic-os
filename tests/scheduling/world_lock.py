"""Seal seeded databases; only fresh clones regain CONNECT."""

import psycopg
from psycopg import sql

from .world_database import admin_url


def connect_grants(database: str) -> tuple[tuple[str, bool], ...]:
    with psycopg.connect(admin_url("postgres"), autocommit=True) as admin:
        rows = admin.execute(
            "SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE "
            "pg_get_userbyid(a.grantee) END,a.is_grantable FROM pg_database d,"
            "aclexplode(d.datacl) a WHERE d.datname=%s "
            "AND a.privilege_type='CONNECT' ORDER BY 1,2",
            [database],
        ).fetchall()
    return tuple((str(role), bool(grantable)) for role, grantable in rows)


def lock(database: str) -> tuple[tuple[str, bool], ...]:
    grants = connect_grants(database)
    with psycopg.connect(admin_url("postgres"), autocommit=True) as admin:
        admin.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                sql.Identifier(database)
            )
        )
        for role, _ in grants:
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    sql.Identifier(database),
                    sql.SQL("PUBLIC") if role == "PUBLIC" else sql.Identifier(role),
                )
            )
    assert not connect_grants(database), "a template CONNECT grant survived"
    try:
        psycopg.connect(admin_url(database)).close()
    except psycopg.OperationalError as error:
        if "not currently accepting connections" not in str(error):
            raise
    else:
        message = "sealed template accepted a connection"
        raise AssertionError(message)
    return grants


def reconnect(database: str, grants: tuple[tuple[str, bool], ...]) -> None:
    with psycopg.connect(admin_url("postgres"), autocommit=True) as admin:
        for role, grantable in grants:
            admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}{}").format(
                    sql.Identifier(database),
                    sql.SQL("PUBLIC") if role == "PUBLIC" else sql.Identifier(role),
                    sql.SQL(" WITH GRANT OPTION" if grantable else ""),
                )
            )
