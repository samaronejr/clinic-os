"""Closed dependency-linked stack specification for production HTTPS QA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ops.testing.https_contract import DEFAULT_COMMAND, TLS_COMMAND
from ops.testing.https_service_specs import (
    ApplicationServiceInput,
    HttpsCredentials,
    application_environment,
    application_service,
    credentials_for,
    database_environment,
    database_service,
    json_strings,
    json_values,
)

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

from ops.testing.tls_contract import RESTORE_DATABASE_HOST, SOURCE_DATABASE_HOST


@dataclass(frozen=True, slots=True)
class HttpsStackInput:
    """Owner and image identities required to derive a stack reservation."""

    claim_id: str
    materializer_claim_id: str
    materializer_volumes: dict[str, str]
    project: str
    postgres_image_id: str
    application_image_id: str
    application_contract: JsonObject
    ca_export_claim_id: str
    phase: str


@dataclass(frozen=True, slots=True)
class HttpsStackPlan:
    """Reserved bytes and exact runtime values for one claimed HTTPS stack."""

    credentials: HttpsCredentials
    database_name: str
    environments: dict[str, dict[str, str]]
    network_name: str
    pgdata_name: str
    cleartext_port: int
    https_port: int
    database_host: str
    phase: str
    spec: JsonObject


def build_https_stack_plan(value: HttpsStackInput) -> HttpsStackPlan:
    """Build the complete reservation before the first Docker mutation."""
    if value.phase not in {"restore", "source"}:
        raise _HttpsStackError
    database_host = (
        SOURCE_DATABASE_HOST if value.phase == "source" else RESTORE_DATABASE_HOST
    )
    tls_role = f"phase1a-{value.phase}-db-tls"
    database_name = f"{value.project}_db"
    network_name = f"{value.project}_network"
    pgdata_name = f"{value.project}_pgdata"
    cleartext_port, https_port = _ports(value.claim_id)
    trust = value.materializer_volumes["phase1a-db-trust"]
    database_tls = value.materializer_volumes[tls_role]
    web_tls = value.materializer_volumes["phase1a-web-tls"]
    credentials = credentials_for(value.claim_id)
    environments = {
        "database": database_environment(database_name, credentials),
        "release": application_environment(
            database_name, database_host, "release", credentials
        ),
        "web": application_environment(
            database_name,
            database_host,
            "web",
            credentials,
        ),
        "cleartext": application_environment(
            database_name, database_host, "web", credentials
        ),
    }
    labels: list[JsonValue] = [
        {"name": "clinic.phase1a.claim", "value": value.claim_id}
    ]
    cleartext_mapping = _published(cleartext_port, 8000)
    https_mapping = _published(https_port, 8443)
    services = [
        application_service(
            ApplicationServiceInput(
                "cleartext",
                list(DEFAULT_COMMAND),
                value.application_image_id,
                value.application_contract,
                network_name,
                trust,
                environments["cleartext"],
                [cleartext_mapping],
            )
        ),
        database_service(
            value.postgres_image_id,
            network_name,
            pgdata_name,
            (database_tls, database_host),
            environments["database"],
        ),
        application_service(
            ApplicationServiceInput(
                "release",
                ["sleep", "infinity"],
                value.application_image_id,
                value.application_contract,
                network_name,
                trust,
                environments["release"],
                [],
            )
        ),
        application_service(
            ApplicationServiceInput(
                "web",
                list(TLS_COMMAND),
                value.application_image_id,
                value.application_contract,
                network_name,
                trust,
                environments["web"],
                [https_mapping],
                web_tls,
            )
        ),
    ]
    borrowed = sorted(
        (
            _borrowed(value.materializer_claim_id, trust),
            _borrowed(value.materializer_claim_id, database_tls),
            _borrowed(value.materializer_claim_id, web_tls),
        ),
        key=lambda item: str(item["volume_name"]),
    )
    desired: JsonObject = {
        "borrowed_network_refs": [],
        "borrowed_volume_refs": json_values(borrowed),
        "database_names": json_strings([database_name]),
        "loopback_ports": json_values(
            [
                _reserved_port(cleartext_port),
                _reserved_port(https_port),
            ]
        ),
        "owned_networks": [
            {
                "attachable": False,
                "driver": "bridge",
                "internal": False,
                "labels": labels,
                "network_name": network_name,
            }
        ],
        "owned_volumes": [
            {"driver": "local", "labels": labels, "volume_name": pgdata_name}
        ],
        "project": value.project,
        "services": json_values(services),
    }
    spec: JsonObject = {
        "claim_id": value.claim_id,
        "dependency_claim_ids": json_strings(
            sorted([value.ca_export_claim_id, value.materializer_claim_id])
        ),
        "desired": desired,
        "kind": "stack",
        "purpose": f"production-image-https-{value.phase}",
    }
    return HttpsStackPlan(
        credentials,
        database_name,
        environments,
        network_name,
        pgdata_name,
        cleartext_port,
        https_port,
        database_host,
        value.phase,
        spec,
    )


def _borrowed(owner: str, volume: str) -> JsonObject:
    return {"access": "read-only", "owner_claim_id": owner, "volume_name": volume}


def _ports(claim_id: str) -> tuple[int, int]:
    token = int(claim_id.split("-", 1)[0], 16)
    return 30_000 + token % 10_000, 50_000 + token % 10_000


def _reserved_port(port: int) -> JsonObject:
    return {"host": "127.0.0.1", "port": port, "transport": "tcp"}


def _published(port: int, container_port: int) -> JsonObject:
    return {
        "container_port": container_port,
        "host": "127.0.0.1",
        "port": port,
        "transport": "tcp",
    }


class _HttpsStackError(ValueError):
    pass
