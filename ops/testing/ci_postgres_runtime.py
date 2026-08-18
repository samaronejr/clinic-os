"""Claimed TLS PostgreSQL resource lifecycle for hosted quality jobs."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never
from urllib.parse import quote, urlencode
from uuid import uuid4

from ops.testing.ci_postgres_stack import (
    _CiDatabaseSpec,
    _create_container,
    _remove,
    _remove_container,
    _reserve,
    _spec,
    _unused_port,
    _wait,
)
from ops.testing.https_service_specs import (
    credentials_for,
    database_environment,
    database_service,
)
from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.isolation_reconcile import reconcile_same_boot
from ops.testing.isolation_refresh import verify_claim
from ops.testing.tls_contract import RESTORE_DATABASE_HOST, SOURCE_DATABASE_HOST

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ops.testing.tls_export import PublicCaExport
    from ops.testing.tls_materializer import MaterializerLease


class _CiDatabaseRuntimeError(RuntimeError):
    pass


def _fail(reason: str) -> Never:
    raise _CiDatabaseRuntimeError(reason)


@dataclass(frozen=True, slots=True)
class CiDatabaseLease:
    """Synthetic role and endpoint values for one active claimed database."""

    app_password: str
    ca_path: Path
    claim_id: str
    container_id: str
    database: str
    host_port: int
    owner_password: str
    postgres_password: str
    super_password: str
    database_host: str = SOURCE_DATABASE_HOST


@contextmanager
def ci_database_lease(
    repository: Path,
    materializer: MaterializerLease,
    public_ca: PublicCaExport,
    database_kind: str = "source",
) -> Iterator[CiDatabaseLease]:
    """Reserve before creating a unique TLS database and reverse-release it."""
    claim_id = str(uuid4())
    token = claim_id.split("-", 1)[0]
    project = f"clinic_phase1a_ci_{token}"
    network = f"{project}_network"
    pgdata = f"{project}_pgdata"
    database = f"clinic_{token}"
    port = _unused_port()
    credentials = credentials_for(claim_id)
    environment = database_environment(database, credentials)
    if database_kind not in {"source", "restore"}:
        _fail("CI PostgreSQL database kind is invalid")
    database_host = (
        SOURCE_DATABASE_HOST if database_kind == "source" else RESTORE_DATABASE_HOST
    )
    tls_volume = materializer.volume_names[f"phase1a-{database_kind}-db-tls"]
    service = database_service(
        materializer.image_id,
        network,
        pgdata,
        (tls_volume, database_host),
        environment,
    )
    service["published_ports"] = [
        {"container_port": 5432, "host": "127.0.0.1", "port": port, "transport": "tcp"}
    ]
    spec = _spec(
        _CiDatabaseSpec(
            claim_id,
            materializer.claim_id,
            public_ca.claim_id,
            project,
            network,
            pgdata,
            database,
            port,
            tls_volume,
            service,
        )
    )
    ledger = (repository / ".omo/evidence/isolation-ledger-phase1a.json").resolve(
        strict=True
    )
    _reserve(ledger, spec)
    container_id = ""
    try:
        label = f"clinic.phase1a.claim={claim_id}"
        run_docker_command(("volume", "create", "--label", label, pgdata))
        run_docker_command(
            ("network", "create", "--driver", "bridge", "--label", label, network)
        )
        container_id = _create_container(project, claim_id, service, environment)
        run_docker_command(("start", container_id))
        _wait(container_id)
        reconcile_same_boot(ledger)
        verify_claim(ledger, claim_id, refresh=True)
        yield CiDatabaseLease(
            credentials.app,
            public_ca.path,
            claim_id,
            container_id,
            database,
            port,
            credentials.owner,
            credentials.postgres,
            credentials.runtime,
            database_host,
        )
    finally:
        if container_id:
            _remove_container(container_id)
        _remove("network", network)
        _remove("volume", pgdata)
        reconcile_same_boot(ledger)


def write_environment(path: Path, lease: CiDatabaseLease) -> None:
    """Write the source-only shell environment consumed by the same job."""
    values = {
        "APP_DATABASE_URL": database_url("clinic_app", lease.app_password, lease),
        "CLINIC_APP_PASSWORD": lease.app_password,
        "CLINIC_OWNER_PASSWORD": lease.owner_password,
        "CLINIC_SUPER_PASSWORD": lease.super_password,
        "MIGRATION_DATABASE_URL": database_url(
            "clinic_owner", lease.owner_password, lease
        ),
        "POSTGRES_CONTAINER": lease.container_id,
        "POSTGRES_DB": lease.database,
        "POSTGRES_HOST_IP": "127.0.0.1",
        "POSTGRES_HOST_PORT": str(lease.host_port),
        "POSTGRES_PASSWORD": lease.postgres_password,
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "postgres",
        "TEST_DATABASE_NAME": f"test_{lease.database}",
        "TEST_SUPERUSER_DATABASE_URL": database_url(
            "clinic_super", lease.super_password, lease
        ),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        raw = "".join(
            f"export {key}='{value}'\n" for key, value in sorted(values.items())
        )
        os.write(descriptor, raw.encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def database_url(role: str, password: str, lease: CiDatabaseLease) -> str:
    """Build one host-loopback verify-full URL for the lease's DNS identity."""
    query = urlencode(
        {
            "hostaddr": "127.0.0.1",
            "sslmode": "verify-full",
            "sslrootcert": str(lease.ca_path),
        }
    )
    encoded_password = quote(password, safe="")
    return (
        f"postgresql://{role}:{encoded_password}@{lease.database_host}:"
        f"{lease.host_port}/{lease.database}?{query}"
    )
