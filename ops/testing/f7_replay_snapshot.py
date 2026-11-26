"""Fingerprint and restore a disposable, post-upgrade PostgreSQL baseline."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg
from psycopg import sql


def _database(url: str, name: str) -> str:
    return urlsplit(url)._replace(path="/" + name).geturl()


def snapshot_digest(dsn: str) -> str:
    """Hash application catalog metadata and rows in stable order."""
    digest = hashlib.sha256()
    with psycopg.connect(dsn) as conn:
        tables = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname IN ('clinic_app', 'public', 'upgrade_rehearsal') "
            "ORDER BY 1, 2"
        ).fetchall()
        for schema, table in tables:
            catalog = conn.execute(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position",
                (schema, table),
            ).fetchall()
            digest.update(json.dumps([schema, table, catalog]).encode())
            relation = sql.Identifier(schema, table)
            for (row,) in conn.execute(
                sql.SQL("SELECT to_jsonb(t)::text FROM {} t ORDER BY 1").format(
                    relation
                )
            ):
                digest.update(row.encode() + b"\n")
    return digest.hexdigest()


@contextmanager
def restore(
    environment: dict[str, str], suffix: str, expected: str
) -> Iterator[dict[str, str]]:
    """Rehearse in an independent copy of the same disposable database."""
    dsn = environment["UPGRADE_ADMIN_DATABASE_URL"]
    source = urlsplit(dsn).path[1:]
    if not source.startswith("upgrade_rc_"):
        message = "F7 replay requires a disposable upgrade database"
        raise ValueError(message)
    name = f"{source}_{suffix}"
    identifier = sql.Identifier(name)
    with psycopg.connect(_database(dsn, "postgres"), autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                identifier, sql.Identifier(source)
            )
        )
        try:
            restored = {
                key: _database(value, name) if key.endswith("_URL") else value
                for key, value in environment.items()
            }
            if snapshot_digest(restored["UPGRADE_ADMIN_DATABASE_URL"]) != expected:
                message = "restored F7 replay snapshot digest does not match"
                raise RuntimeError(message)
            yield restored
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(identifier))
