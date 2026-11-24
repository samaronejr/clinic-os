"""Read-only binding of a completed fixture receipt to the rehearsed database."""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Never, TypedDict, cast

from django.db import connection

if TYPE_CHECKING:
    from collections.abc import Mapping


class BaseObservation(TypedDict):
    """Identity and fixture requirements established before native seeding."""

    database: list[str | int]
    base_sha: str
    required: list[str]


def partial() -> Never:
    """Refuse use of a partially seeded database."""
    message = "partial seed: rebuild database"
    raise RuntimeError(message)


def database_identity() -> list[str | int]:
    """Read the server-assigned identity of this scratch database."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), oid FROM pg_database "
            "WHERE datname=current_database()"
        )
        return list(cursor.fetchone())


def observation_path() -> Path:
    """Return the runner-owned location of the base observation."""
    return Path(os.environ["UPGRADE_BASE_OBSERVATION"])


def observed_base() -> BaseObservation:
    """Check the base observation against the current database and base SHA."""
    path = observation_path()
    if not path.is_file():
        partial()
    observed = cast("BaseObservation", json.loads(path.read_text(encoding="utf-8")))
    if (
        observed["database"] != database_identity()
        or observed["base_sha"] != os.environ["UPGRADE_BASE_SHA"]
    ):
        partial()
    return observed


def check_histories(markers: "Mapping[str, object]", required: list[str]) -> None:
    """Refuse receipt histories different from those required by the base."""
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


def require_completed_seed(contents: str) -> list[str]:
    """Bind a completed fixture receipt to the observed base."""
    markers = json.loads(contents)
    base = observed_base()
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regnamespace('upgrade_rehearsal') IS NOT NULL")
        started = cursor.fetchone() == (True,)
    if not started or markers["database"] != base["database"]:
        partial()
    check_histories(markers, base["required"])
    return base["required"]
