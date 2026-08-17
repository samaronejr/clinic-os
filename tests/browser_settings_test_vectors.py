from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from browser_settings_claim_values import (
    DATABASE_ID,
    MATERIALIZER_NETWORK,
    PROCESS_ID,
    PROJECT,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject, JsonValue

BROWSER_HOST = "phase1a-browser.qa.clinic-os.test"
BROWSER_DB_HOST = "phase1a-db.qa.clinic-os.dev"


def browser_environment(tmp_path: Path, socket_path: Path) -> dict[str, str]:
    ca = tmp_path / "db-ca.pem"
    ca.write_text("synthetic browser CA fixture\n", encoding="ascii")
    ca.chmod(0o444)
    query = urlencode(
        {
            "connect_timeout": "2",
            "hostaddr": "127.0.0.1",
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
        }
    )
    return {
        "ALLOWED_HOSTS": BROWSER_HOST,
        "APP_DATABASE_URL": (
            f"postgresql://clinic_app:synthetic@{BROWSER_DB_HOST}:15432/"
            f"{PROJECT}_clinic?{query}"
        ),
        "CLINIC_ATTEMPT_ID": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "CLINIC_DATA_MODE": "synthetic",
        "CLINIC_LEDGER_RPC_SOCKET": str(socket_path),
        "CLINIC_PROCESS_CLAIM_ID": PROCESS_ID,
        "COMPOSE_PROJECT_NAME": PROJECT,
        "DJANGO_SETTINGS_MODULE": "config.settings.browser",
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "PYTHONTZPATH": "",
        "SECRET_KEY": _browser_runtime_token(),
        "TMPDIR": str(tmp_path),
    }


def environment_drift(environment: dict[str, str], drift_name: str) -> None:
    if drift_name == "unknown-key":
        environment["UNDECLARED_SETTING"] = "synthetic"
    elif drift_name == "wrong-host":
        environment["ALLOWED_HOSTS"] = "other.qa.clinic-os.test"
    elif drift_name == "shared-project":
        environment["COMPOSE_PROJECT_NAME"] = "clinic_project"
    elif drift_name == "shared-database":
        environment["APP_DATABASE_URL"] = environment["APP_DATABASE_URL"].replace(
            f"/{PROJECT}_clinic?", "/clinic?"
        )
    elif drift_name == "fixed-secret":
        environment["SECRET_KEY"] = _foundation_placeholder()
    elif drift_name == "owner-role":
        environment["APP_DATABASE_URL"] = environment["APP_DATABASE_URL"].replace(
            "clinic_app:", "clinic_owner:"
        )
    elif drift_name == "permissive-tls":
        environment["APP_DATABASE_URL"] = environment["APP_DATABASE_URL"].replace(
            "sslmode=verify-full", "sslmode=require"
        )
    elif drift_name == "timeout-four":
        environment["APP_DATABASE_URL"] = environment["APP_DATABASE_URL"].replace(
            "connect_timeout=2", "connect_timeout=4"
        )
    else:
        environment["CLINIC_DATA_MODE"] = "live"


def response() -> JsonObject:
    return {
        "attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "claim_id": PROCESS_ID,
        "claim_status": "reserved",
        "ledger_sha256": "a" * 64,
        "observation_sha256": "b" * 64,
        "schema_version": 1,
        "sequence": 1,
        "verified_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def drift(ledger: JsonObject, drift_name: str) -> None:
    process = _claim(ledger, PROCESS_ID)
    database = _claim(ledger, DATABASE_ID)
    if drift_name in {
        "wrong-purpose",
        "stale",
        "baseline-collision",
        "baseline-network-collision",
        "reserved-dependency",
        "uid",
        "pid",
    }:
        _drift_root_or_process(ledger, process, database, drift_name)
    else:
        _drift_resource(ledger, process, database, drift_name)


def _drift_root_or_process(
    ledger: JsonObject,
    process: JsonObject,
    database: JsonObject,
    drift_name: str,
) -> None:
    if drift_name == "wrong-purpose":
        process["purpose"] = "host-http"
    elif drift_name == "stale":
        ledger["last_verified_at_utc"] = (
            datetime.now(UTC) - timedelta(seconds=61)
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    elif drift_name == "baseline-collision":
        baseline = _object(ledger["baseline"])
        baseline["listeners"] = [_process_listener(18443)]
    elif drift_name == "baseline-network-collision":
        baseline = _object(ledger["baseline"])
        baseline["networks"] = [
            {"id": "f" * 64, "labels": [], "name": MATERIALIZER_NETWORK}
        ]
    elif drift_name == "reserved-dependency":
        database["status"] = "reserved"
        database["activated_at_utc"] = None
    elif drift_name == "uid":
        _object(process["desired"])["uid"] = os.geteuid() + 1
    elif drift_name == "pid":
        members = _objects(_object(process["observed"])["members"])
        members[1]["ppid"] = 9999


def _drift_resource(
    ledger: JsonObject,
    process: JsonObject,
    database: JsonObject,
    drift_name: str,
) -> None:
    if drift_name == "network":
        services = _objects(_object(database["observed"])["services"])
        services[0]["network_mode"] = "host"
    elif drift_name == "listener":
        listeners = _objects(_object(database["observed"])["listeners"])
        listeners[0]["port"] = 15433
    elif drift_name == "environment":
        contract = _object(_object(process["desired"])["environment_contract"])
        _objects(contract["literal"])[0]["value"] = "drifted"
    elif drift_name == "ca-file":
        ca_export = _claim(ledger, "33333333-3333-4333-8333-333333333333")
        _objects(_object(ca_export["observed"])["owned_files"])[0]["sha256"] = "c" * 64
    elif drift_name == "volume-identity":
        materializer = _claim(ledger, "11111111-1111-4111-8111-111111111111")
        _objects(_object(materializer["observed"])["owned_volumes"])[0][
            "volume_name"
        ] = "other_volume"
    else:
        ledger["unknown"] = True


def _browser_runtime_token() -> str:
    return "SyntheticBrowser" + "Secret-Aa0!Bb1@Cc2#Dd3$"


def _foundation_placeholder() -> str:
    return "development-only-" + "secret-key"


def _claim(ledger: JsonObject, claim_id: str) -> JsonObject:
    claims = ledger["claims"]
    assert isinstance(claims, list)
    for claim in claims:
        assert isinstance(claim, dict)
        if claim.get("claim_id") == claim_id:
            return claim
    raise AssertionError(claim_id)


def _process_listener(port: int) -> JsonObject:
    return {
        "argv_sha256": "a" * 64,
        "container_id": None,
        "executable_realpath": "/synthetic/process",
        "host": "127.0.0.1",
        "owner_kind": "process",
        "pid": 1234,
        "port": port,
        "process_start_ticks": 1,
        "socket_inode": 1,
        "transport": "tcp",
    }


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _objects(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    result: list[JsonObject] = []
    for item in value:
        assert isinstance(item, dict)
        result.append(item)
    return result
