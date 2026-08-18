"""Exact service and environment contracts for production HTTPS QA."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ops.testing.https_contract import application_filesystem_contract
from ops.testing.tls_contract import WEB_HOST

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

PROXY_KEYS = sorted(
    (
        "ALL_PROXY",
        "FORWARDED_ALLOW_IPS",
        "GUNICORN_CMD_ARGS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    )
)


@dataclass(frozen=True, slots=True)
class HttpsCredentials:
    """Claim-derived synthetic credentials that never enter evidence records."""

    app: str
    owner: str
    postgres: str
    runtime: str


@dataclass(frozen=True, slots=True)
class ApplicationServiceInput:
    """Inputs that differ between release and HTTPS web services."""

    name: str
    command: list[str]
    image_id: str
    image_contract: JsonObject
    network: str
    trust: str
    environment: dict[str, str]
    published_ports: list[JsonObject]
    web_tls: str | None = None


def credentials_for(claim_id: str) -> HttpsCredentials:
    """Derive unique synthetic secrets without fixed credential literals."""
    digest = hashlib.sha256(claim_id.encode()).hexdigest()
    return HttpsCredentials(
        f"Aa1app{digest}",
        f"Bb2owner{digest}",
        f"Cc3postgres{digest}",
        f"Aa1!Zz9?Qq7#{digest}",
    )


def database_environment(
    database: str,
    credentials: HttpsCredentials,
) -> dict[str, str]:
    """Return the pinned PostgreSQL initialization environment."""
    return {
        "POSTGRES_DB": database,
        "POSTGRES_PASSWORD": credentials.postgres,
        "POSTGRES_USER": "postgres",
    }


def application_environment(
    database: str,
    database_host: str,
    purpose: str,
    credentials: HttpsCredentials,
) -> dict[str, str]:
    """Return one exact production or release process environment."""
    role = "clinic_app" if purpose == "web" else "clinic_owner"
    password = credentials.app if purpose == "web" else credentials.owner
    key = "APP_DATABASE_URL" if purpose == "web" else "MIGRATION_DATABASE_URL"
    settings = "config.settings.prod" if purpose == "web" else "config.settings.release"
    return {
        "ALLOWED_HOSTS": WEB_HOST,
        "CLINIC_DATA_MODE": "synthetic",
        "CLINIC_PROCESS_PURPOSE": purpose,
        "DJANGO_SETTINGS_MODULE": settings,
        key: _database_url(role, password, database, database_host),
        "PYTHONTZPATH": "",
        "SECRET_KEY": credentials.runtime,
        "SECURE_SSL_HOST": f"{WEB_HOST}:8443",
    }


def database_service(
    image_id: str,
    network: str,
    pgdata: str,
    tls_identity: tuple[str, str],
    environment: dict[str, str],
) -> JsonObject:
    """Build the exact TLS PostgreSQL service contract."""
    tls, database_host = tls_identity
    return {
        "command": [
            "postgres",
            "-c",
            "ssl=on",
            "-c",
            "ssl_cert_file=/run/clinic-test-db-tls/tls/tls.crt",
            "-c",
            "ssl_key_file=/run/clinic-test-db-tls/tls/tls.key",
            "-c",
            "listen_addresses=*",
            "-c",
            "hba_file=/run/clinic-test-db-tls/tls/pg_hba.conf",
        ],
        "environment_contract": environment_contract(
            environment,
            {"POSTGRES_PASSWORD"},
        ),
        "extra_hosts": [],
        "filesystem_contract": None,
        "gid": 0,
        "image_contract": None,
        "image_id": image_id,
        "name": "database",
        "network_mode": "bridge",
        "network_refs": [{"aliases": [database_host], "network_name": network}],
        "published_ports": [],
        "start_policy": "running-before-activation",
        "uid": 0,
        "volume_mounts": [
            {
                "read_only": True,
                "target": "/run/clinic-test-db-tls",
                "volume_name": tls,
            },
            {
                "read_only": False,
                "target": "/var/lib/postgresql/data",
                "volume_name": pgdata,
            },
        ],
    }


def application_service(value: ApplicationServiceInput) -> JsonObject:
    """Build one non-root release or HTTPS web service contract."""
    mounts: list[JsonObject] = [
        {
            "read_only": True,
            "target": "/run/clinic-test-db-trust",
            "volume_name": value.trust,
        }
    ]
    aliases = [f"{value.name}.qa.clinic-os.dev"]
    if value.web_tls is not None:
        mounts.append(
            {
                "read_only": True,
                "target": "/run/clinic-test-tls",
                "volume_name": value.web_tls,
            }
        )
        aliases = [WEB_HOST]
    mounts.sort(key=lambda item: str(item["target"]))
    network_reference: JsonObject = {
        "aliases": json_strings(aliases),
        "network_name": value.network,
    }
    return {
        "command": json_strings(value.command),
        "environment_contract": environment_contract(
            value.environment,
            {"APP_DATABASE_URL", "MIGRATION_DATABASE_URL", "SECRET_KEY"},
        ),
        "extra_hosts": [],
        "filesystem_contract": application_filesystem_contract(),
        "gid": 10001,
        "image_contract": value.image_contract,
        "image_id": value.image_id,
        "name": value.name,
        "network_mode": "bridge",
        "network_refs": [network_reference],
        "published_ports": json_values(value.published_ports),
        "start_policy": "running-before-activation",
        "uid": 10001,
        "volume_mounts": json_values(mounts),
    }


def environment_contract(
    environment: dict[str, str],
    secret_candidates: set[str],
) -> JsonObject:
    """Separate literal and secret names without serializing secret values."""
    secret = sorted(set(environment) & secret_candidates)
    literal: list[JsonValue] = [
        {"name": name, "value": value}
        for name, value in sorted(environment.items())
        if name not in secret
    ]
    return {
        "absent_keys": json_strings(PROXY_KEYS),
        "literal": literal,
        "secret_keys": json_strings(secret),
    }


def json_values(objects: list[JsonObject]) -> list[JsonValue]:
    """Widen invariant object lists for recursive JSON values."""
    values: list[JsonValue] = []
    values.extend(objects)
    return values


def json_strings(strings: list[str]) -> list[JsonValue]:
    """Widen invariant string lists for recursive JSON values."""
    values: list[JsonValue] = []
    values.extend(strings)
    return values


def _database_url(
    role: str,
    password: str,
    database: str,
    database_host: str,
) -> str:
    return (
        f"postgresql://{role}:{password}@{database_host}:5432/{database}"
        "?connect_timeout=3&sslmode=verify-full"
        "&sslrootcert=/run/clinic-test-db-trust/db-ca.pem"
    )
