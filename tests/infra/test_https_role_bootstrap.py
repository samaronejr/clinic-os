from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

import psycopg
import pytest
from ops.testing import https_role_bootstrap
from ops.testing.https_stack_specs import HttpsStackInput, build_https_stack_plan
from ops.testing.tls_specs import materializer_volume_names

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject

pytestmark = pytest.mark.django_db(transaction=True)


class _FixtureLookup(Protocol):
    def getfixturevalue(self, argname: str) -> object: ...


def test_https_bootstrap_installs_migration_roles_with_closed_authority(
    monkeypatch: pytest.MonkeyPatch,
    request: _FixtureLookup,
) -> None:
    # Given the real bootstrap on a transaction-isolated role/schema namespace.
    superuser_database_url: object = request.getfixturevalue("superuser_database_url")
    assert isinstance(superuser_database_url, str)
    prefix = f"tls_{uuid4().hex}_"
    claim = "11111111-1111-4111-8111-111111111111"
    materializer = "22222222-2222-4222-8222-222222222222"
    contract: JsonObject = {
        "available_suite_ids": [],
        "kind": "application",
        "revision_sha": "3" * 40,
        "source_entry_count": 1,
        "source_manifest_sha256": "4" * 64,
        "tree_sha": "5" * 40,
    }
    plan = build_https_stack_plan(
        HttpsStackInput(
            claim,
            materializer,
            materializer_volume_names("clinic_tls", materializer),
            "clinic_https_fixture",
            f"sha256:{'6' * 64}",
            f"sha256:{'7' * 64}",
            contract,
            "33333333-3333-4333-8333-333333333333",
            "source",
        )
    )
    roles = ("owner", "app", "resolver", "super", "agent")
    with (
        psycopg.connect(superuser_database_url) as connection,
        connection.transaction(force_rollback=True),
    ):

        def execute_bootstrap(arguments: tuple[str, ...]) -> str:
            assert arguments[:4] == ("exec", "--user", "999:999", "synthetic-pg")
            assert arguments[-2] == "-c"
            statement = re.sub(
                r"\bclinic_(owner|app|resolver|super|agent)\b",
                lambda match: prefix + match[0],
                arguments[-1],
            )
            _ = connection.execute(statement.encode("utf-8"))
            return ""

        monkeypatch.setattr(
            https_role_bootstrap, "run_docker_command", execute_bootstrap
        )
        # When the actual harness SQL executes against PostgreSQL, not a SQL fake.
        https_role_bootstrap.configure_roles(
            "synthetic-pg", replace(plan, database_name=connection.info.dbname)
        )

        # Then every migration prerequisite exists with canonical role authority.
        actual = connection.execute(
            """SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolinherit
               FROM pg_roles WHERE rolname = ANY(%s)""",
            ([f"{prefix}clinic_{role}" for role in roles],),
        ).fetchall()
        assert set(actual) == {
            (f"{prefix}clinic_owner", True, False, False, True),
            (f"{prefix}clinic_app", True, False, False, True),
            (f"{prefix}clinic_resolver", False, False, True, True),
            (f"{prefix}clinic_super", True, True, False, True),
            (f"{prefix}clinic_agent", True, False, False, False),
        }
        assert connection.execute(
            """SELECT has_schema_privilege(%s, %s, 'USAGE'),
               has_database_privilege(%s, current_database(), 'CONNECT'),
               rolpassword IS NULL FROM pg_authid WHERE rolname = %s""",
            (
                f"{prefix}clinic_agent",
                f"{prefix}clinic_app",
                f"{prefix}clinic_agent",
                f"{prefix}clinic_agent",
            ),
        ).fetchone() == (True, True, True)
