"""Keep the base schema observation and seed checkpoint in the rehearsal DB."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from django.db import connection, transaction

if TYPE_CHECKING:
    from collections.abc import Mapping


def database_identity() -> list[str | int]:
    """Bind an exported receipt to this scratch database."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), oid FROM pg_database "
            "WHERE datname=current_database()"
        )
        return list(cursor.fetchone())


def _required_histories() -> list[str]:
    """Observe the migrated base, independently of fixture dispatch and output."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('clinic_app.ai_aiinvocation') IS NOT NULL "
            "OR EXISTS (SELECT FROM clinic_app.django_migrations "
            "WHERE app='ai' AND name='0014_staged_rollout'), "
            "to_regclass('clinic_app.comms_message') IS NOT NULL "
            "AND NOT EXISTS (SELECT FROM pg_attribute WHERE "
            "attrelid=to_regclass('clinic_app.comms_integrationoperation') "
            "AND attname='attempt_sequence' AND attnum>0 AND NOT attisdropped)"
        )
        ai, pre_sequence_inbox = cursor.fetchone()
    required = ["core"]
    # The inbox verifier is introduced by the inbox migration lane. Once it
    # ships, its legacy histories are mandatory on every pre-sequence base.
    if (
        pre_sequence_inbox
        and Path(__file__).with_name("migration_upgrade_inbox_verify.py").is_file()
    ):
        required.append("inbox")
    if ai:
        required.append("ai")
    return required


def begin_seed() -> tuple[list[str], str | None]:
    """Claim the scratch DB once, or recover an already committed seed."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(1037641436)")
        cursor.execute("CREATE SCHEMA IF NOT EXISTS upgrade_rehearsal")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS upgrade_rehearsal.seed ("
            "singleton boolean PRIMARY KEY CHECK (singleton), "
            "required text[] NOT NULL, markers text)"
        )
        cursor.execute("SELECT required, markers FROM upgrade_rehearsal.seed")
        row = cursor.fetchone()
        if row is not None:
            required, recorded = row
            if recorded is None:
                message = "upgrade seed has an incomplete database checkpoint"
                raise RuntimeError(message)
            return cast("tuple[list[str], str]", (required, recorded))
        observed = _required_histories()
        cursor.execute(
            "INSERT INTO upgrade_rehearsal.seed (singleton, required) "
            "VALUES (true, %s)",
            [observed],
        )
    return observed, None


def _check_histories(markers: "Mapping[str, object]", required: list[str]) -> None:
    actual = ["core"]
    actual.extend(
        name
        for name in ("inbox", "ai")
        if isinstance(markers.get(name), dict) and markers[name]
    )
    if actual != required:
        message = f"upgrade seed coverage mismatch: {required=}, {actual=}"
        raise RuntimeError(message)
    if markers.get("ai_present") is not ("ai" in required):
        message = "AI seed presence disagrees with the observed base schema"
        raise RuntimeError(message)
    if "seeds" in markers and markers["seeds"] != required:
        message = "declared upgrade seeds disagree with the observed base schema"
        raise RuntimeError(message)


def checkpoint_seed(markers: "Mapping[str, object]", required: list[str]) -> str:
    """Commit full markers before attempting the fallible file publication."""
    _check_histories(markers, required)
    contents = json.dumps(markers, indent=2, sort_keys=True) + "\n"
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE upgrade_rehearsal.seed SET markers=%s "
            "WHERE singleton AND required=%s AND markers IS NULL",
            [contents, required],
        )
        if cursor.rowcount != 1:
            message = "cannot commit the upgrade seed checkpoint"
            raise RuntimeError(message)
    return contents


def publish_seed(path: Path, contents: str) -> None:
    """Publish, or replay, the exact completed database checkpoint."""
    pending = path.with_suffix(".pending")
    pending.write_text(contents, encoding="utf-8")
    pending.replace(path)


def require_completed_seed(contents: str) -> list[str]:
    """Refuse a modified or incomplete receipt before any forward checks."""
    markers = json.loads(contents)
    if markers["database"] != database_identity():
        message = "completed upgrade seed belongs to a different database"
        raise RuntimeError(message)
    with connection.cursor() as cursor:
        cursor.execute("SELECT required, markers FROM upgrade_rehearsal.seed")
        row = cursor.fetchone()
    if row is None:
        message = "missing upgrade seed database checkpoint"
        raise RuntimeError(message)
    required, recorded = row
    _check_histories(markers, required)
    if contents != recorded:
        message = "seed receipt disagrees with the completed database checkpoint"
        raise RuntimeError(message)
    return cast("list[str]", required)
