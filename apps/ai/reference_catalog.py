"""Deterministic, versioned licensed references; never model-supplied URLs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path

from apps.ai.contracts import GatewayRefusedError

MAX_CATALOG_ENTRIES = 25
MAX_CATALOG_TEXT = 2000


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """A source statement is distinct from a patient fact or model hypothesis."""

    key: str
    version: int
    source: str
    published_on: str
    population: str
    statement: str
    limitations: str
    license_ref: str

    def reference(self) -> dict[str, str | int]:
        """Bind the complete licensed entry, not just its identifier."""
        return {
            **asdict(self),
            "digest": sha256(
                json.dumps(asdict(self), sort_keys=True).encode()
            ).hexdigest(),
        }


def load_catalog() -> tuple[CatalogEntry, ...]:
    """Only the shipped synthetic CC0 catalog is available in this release."""
    try:
        raw = json.loads(
            (Path(__file__).parent / "fixtures/reference-catalog-v1.json").read_text(
                encoding="utf-8"
            )
        )
        entries = tuple(CatalogEntry(**entry) for entry in raw["entries"])
        dates = tuple(
            date.fromisoformat(entry.published_on).isoformat() for entry in entries
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise GatewayRefusedError(reason="configuration_unavailable") from error
    if (
        set(raw) != {"schema", "entries"}
        or raw["schema"] != "synthetic-reference-catalog-v1"
        or not entries
        or len(entries) > MAX_CATALOG_ENTRIES
        or any(
            type(entry.version) is not int
            or entry.version < 1
            or any(
                not isinstance(value, str) or not value or len(value) > MAX_CATALOG_TEXT
                for key, value in asdict(entry).items()
                if key != "version"
            )
            or published != entry.published_on
            or entry.license_ref != "CC0-1.0:clinic-os-synthetic-v1"
            for entry, published in zip(entries, dates, strict=True)
        )
    ):
        raise GatewayRefusedError(reason="configuration_unavailable")
    if len({entry.key for entry in entries}) != len(entries):
        raise GatewayRefusedError(reason="configuration_unavailable")
    return entries
