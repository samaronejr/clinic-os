"""Synthetic database role bootstrap for the isolated HTTPS acceptance stack."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ops.testing.isolation_docker_metadata import run_docker_command

if TYPE_CHECKING:
    from ops.testing.https_stack_specs import HttpsStackPlan


def configure_roles(container_id: str, plan: HttpsStackPlan) -> None:
    """Install the production role posture required by existing migrations."""
    sql = (
        f"CREATE ROLE clinic_owner LOGIN PASSWORD '{plan.credentials.owner}';"
        f"CREATE ROLE clinic_app LOGIN PASSWORD '{plan.credentials.app}';"
        "CREATE ROLE clinic_resolver NOLOGIN BYPASSRLS;"
        "ALTER ROLE clinic_owner WITH LOGIN NOSUPERUSER NOBYPASSRLS "
        "NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT;"
        "ALTER ROLE clinic_app WITH LOGIN NOSUPERUSER NOBYPASSRLS "
        "NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT;"
        "ALTER ROLE clinic_resolver WITH NOLOGIN NOSUPERUSER BYPASSRLS "
        "NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT;"
        f"ALTER DATABASE {plan.database_name} OWNER TO clinic_owner;"
        f"REVOKE ALL ON DATABASE {plan.database_name} FROM PUBLIC;"
        f"GRANT CONNECT ON DATABASE {plan.database_name} "
        "TO clinic_owner, clinic_app;"
        "CREATE SCHEMA clinic_app AUTHORIZATION clinic_owner;"
        "REVOKE ALL ON SCHEMA clinic_app FROM PUBLIC, clinic_app, clinic_resolver;"
        "GRANT USAGE ON SCHEMA clinic_app TO clinic_owner, clinic_app;"
        "GRANT USAGE, CREATE ON SCHEMA clinic_app TO clinic_resolver;"
        "GRANT clinic_app TO clinic_owner WITH INHERIT FALSE, SET TRUE;"
        "GRANT clinic_resolver TO clinic_owner WITH INHERIT FALSE, SET TRUE;"
        "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA clinic_app "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clinic_app;"
        "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA clinic_app "
        "GRANT USAGE ON SEQUENCES TO clinic_app;"
    )
    run_docker_command(
        (
            "exec",
            "--user",
            "999:999",
            container_id,
            "psql",
            "-U",
            "postgres",
            "-d",
            plan.database_name,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        )
    )
