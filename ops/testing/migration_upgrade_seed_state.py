"""Authorize one seed against a freshly migrated disposable database."""

import json
import os
import sys
from pathlib import Path
from runpy import run_path
from typing import TYPE_CHECKING, Never, TypedDict, cast

import psycopg
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from psycopg import Cursor


class _BaseObservation(TypedDict):
    database: list[str | int]
    base_sha: str
    required: list[str]


def _partial() -> Never:
    message = "partial seed: rebuild database"
    raise RuntimeError(message)


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


def _pristine(*, claim: bool) -> None:
    """Check the live database, serializing inspection and the irrevocable claim."""
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SELECT pg_advisory_xact_lock(1037641436)")
        cursor.execute("SET LOCAL row_security=off")
        cursor.execute(
            "SELECT current_database(), oid FROM pg_database "
            "WHERE datname=current_database()"
        )
        identity = cursor.fetchone()
        if identity is None or list(identity) != database_identity():
            _partial()
        cursor.execute("SELECT to_regnamespace('upgrade_rehearsal') IS NULL")
        if cursor.fetchone() != (True,):
            _partial()
        read_rows = cast(
            "Callable[[Cursor[tuple[object, ...]]], object]",
            run_path(str(Path(__file__).with_name("migration_upgrade_rows.py")))[
                "read_rows"
            ],
        )
        if read_rows(cursor):
            _partial()
        if claim:
            cursor.execute("CREATE SCHEMA upgrade_rehearsal AUTHORIZATION clinic_owner")


def _observation_path() -> Path:
    return Path(os.environ["UPGRADE_BASE_OBSERVATION"])


def observe_base() -> None:
    """Only the runner calls this, after migrating a DB created from template0."""
    executor = MigrationExecutor(connection)
    loader = executor.loader
    loader.check_consistent_history(connection)
    if executor.migration_plan(loader.graph.leaf_nodes()) or (
        loader.applied_migrations.keys() - loader.disk_migrations.keys()
    ):
        message = "upgrade database does not match the expected base migration graph"
        raise RuntimeError(message)
    _pristine(claim=False)
    observed: _BaseObservation = {
        "database": database_identity(),
        "base_sha": os.environ["UPGRADE_BASE_SHA"],
        "required": _required_histories(),
    }
    with _observation_path().open("x", encoding="utf-8") as output:
        json.dump(observed, output)


def _observed_base() -> _BaseObservation:
    path = _observation_path()
    if not path.is_file():
        _partial()
    observed = cast("_BaseObservation", json.loads(path.read_text(encoding="utf-8")))
    if (
        observed["database"] != database_identity()
        or observed["base_sha"] != os.environ["UPGRADE_BASE_SHA"]
    ):
        _partial()
    return observed


def begin_seed(path: Path) -> list[str]:
    """Consume pristine authorization once, before making any fixture writes."""
    if any(
        item.exists() or item.is_symlink()
        for item in (path, path.with_suffix(".pending"))
    ):
        _partial()
    base = _observed_base()
    _pristine(claim=True)
    return base["required"]


def check_histories(markers: "Mapping[str, object]", required: list[str]) -> None:
    """Reject omissions independently of the loader's own presence markers."""
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


def publish_seed(path: Path, contents: str) -> None:
    """Publish the completed fixture markers without allowing same-DB retry."""
    pending = path.with_suffix(".pending")
    try:
        with pending.open("x", encoding="utf-8") as output:
            output.write(contents)
        pending.replace(path)
    except OSError as error:
        message = "seed receipt publication failed; rebuild database"
        print(message, file=sys.stderr)  # noqa: T201 - piped diagnostics
        raise SystemExit(74) from error


def require_completed_seed(contents: str) -> list[str]:
    """Bind fixture markers to the current run's independent base observation."""
    markers = json.loads(contents)
    base = _observed_base()
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regnamespace('upgrade_rehearsal') IS NOT NULL")
        started = cursor.fetchone() == (True,)
    if not started or markers["database"] != base["database"]:
        _partial()
    check_histories(markers, base["required"])
    return base["required"]
