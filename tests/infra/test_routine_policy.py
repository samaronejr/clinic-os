"""PG16 ACL drift tests change metadata only; no privileged routine runs."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from ops.db import posture
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextmanager
def _catalog() -> Iterator[psycopg.Connection]:
    with (
        psycopg.connect(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        db.transaction(force_rollback=True),
    ):
        db.execute(posture.CATALOG_PATH)
        yield db


def _safe(db: psycopg.Connection) -> bool:
    return posture._role_authority_safe(db, list(posture.PINNED_ROLES))


def test_grant_option_is_not_plain_execute() -> None:
    with _catalog() as db:
        assert _safe(db)
        db.execute(
            "GRANT EXECUTE ON FUNCTION clinic_app.data_request_now() "
            "TO clinic_app WITH GRANT OPTION"
        )
        assert not _safe(db)
        db.execute(
            "REVOKE GRANT OPTION FOR EXECUTE ON FUNCTION "
            "clinic_app.data_request_now() FROM clinic_app"
        )
        assert _safe(db)


@pytest.mark.parametrize("public", [True, False], ids=["public-default", "private"])
def test_unknown_overload_is_not_the_reviewed_signature(public: bool) -> None:
    with _catalog() as db:
        assert _safe(db)
        db.execute(
            "CREATE FUNCTION clinic_app.data_request_now(integer) "
            "RETURNS int LANGUAGE SQL RETURN 0"
        )
        if not public:
            db.execute(
                "REVOKE ALL ON FUNCTION clinic_app.data_request_now(integer) "
                "FROM PUBLIC"
            )
            db.execute(
                "GRANT EXECUTE ON FUNCTION clinic_app.data_request_now(integer) "
                "TO clinic_app"
            )
        assert not _safe(db)
        db.execute("DROP FUNCTION clinic_app.data_request_now(integer)")
        assert _safe(db)


def test_reviewed_namespace_checked_without_any_extension() -> None:
    with (
        psycopg.connect(
            os.environ["TEST_SUPERUSER_DATABASE_URL"], dbname="postgres"
        ) as db,
        db.transaction(force_rollback=True),
    ):
        db.execute(posture.CATALOG_PATH)
        assert _safe(db)
        db.execute("CREATE SCHEMA clinic_app AUTHORIZATION clinic_owner")
        db.execute(
            "CREATE FUNCTION clinic_app.data_request_now() "
            "RETURNS int LANGUAGE SQL RETURN 0"
        )
        assert not _safe(db)
        db.execute("DROP FUNCTION clinic_app.data_request_now()")
        assert _safe(db)


@pytest.mark.parametrize("login", ["clinic_app", "clinic_agent"])
def test_reachable_schema_ownership_is_itself_refused(login: str) -> None:
    with _catalog() as db:
        owner, bridge, schema = (sql.Identifier("r4e_" + uuid4().hex) for _ in range(3))
        db.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(owner))
        db.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(bridge))
        db.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(schema, owner))
        db.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE").format(
                owner, bridge
            )
        )
        assert _safe(db)
        db.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT FALSE").format(
                bridge, sql.Identifier(login)
            )
        )
        assert not _safe(db)
        db.execute(sql.SQL("REVOKE {} FROM {}").format(bridge, sql.Identifier(login)))
        assert _safe(db)


@pytest.mark.parametrize(
    "component", ["pg_read_all_stats", "pg_read_all_settings", "pg_stat_scan_tables"]
)
def test_monitor_component_denied_without_its_overlapping_defaults(
    component: str,
) -> None:
    with _catalog() as db:
        role = sql.Identifier(component)
        db.execute(sql.SQL("REVOKE {} FROM pg_monitor").format(role))
        # Isolate the role denial from the stock native routine EXECUTE grants.
        for (signature,) in db.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "WHERE p.pronamespace='pg_catalog'::regnamespace AND EXISTS "
            "(SELECT FROM aclexplode(p.proacl) a WHERE "
            "a.grantee=%s::regrole AND a.privilege_type='EXECUTE')",
            (component,),
        ).fetchall():
            db.execute(
                sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(
                    sql.SQL(signature), role
                )
            )
        assert _safe(db)
        db.execute(sql.SQL("GRANT {} TO clinic_app").format(role))
        assert not _safe(db)
        db.execute(sql.SQL("REVOKE {} FROM clinic_app").format(role))
        assert _safe(db)


def test_policy_rejects_duplicate_signature_and_unapproved_option(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "policy.tsv"
    row = [
        "clinic_app.synthetic(integer)",
        "x integer",
        "clinic_owner",
        "a" * 64,
        "clinic_app,clinic_owner",
        "clinic_app",
        "intake/0001_initial",
        "intake/0001_initial",
        "intake/0001_initial",
        "synthetic parser control",
    ]
    manifest.write_text("\t".join(row) + "\n")
    (entry,) = posture._read_policy(manifest).values()
    assert entry.grantable == {"clinic_app"}
    manifest.write_text(
        "\t".join(row) + "\n" + "\t".join([*row[:3], "b" * 64, *row[4:]]) + "\n"
    )
    with pytest.raises(ValueError, match="ambiguous"):
        posture._read_policy(manifest)
    row[5] = "clinic_agent"
    manifest.write_text("\t".join(row) + "\n")
    with pytest.raises(ValueError, match="invalid"):
        posture._read_policy(manifest)
    row[5] = "-"
    row[1] = "x bigint"
    manifest.write_text("\t".join(row) + "\n")
    with pytest.raises(ValueError, match="invalid"):
        posture._read_policy(manifest)
