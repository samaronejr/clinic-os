"""Bounded offline Anvisa/CMED snapshot ingestion; never a network client."""

from __future__ import annotations

import json
import re
from datetime import date
from hashlib import sha256
from importlib import import_module
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from apps.identity.management.base import assert_owner_database_role
from apps.prescription.models import MedicationCatalogEntry, MedicationCatalogImport

if TYPE_CHECKING:
    from pathlib import Path

    from apps.core.json_schema import SchemaValidator

MAX_SOURCE_BYTES: Final = 32 * 1024 * 1024
SOURCE_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "source_url", "retrieved_on", "entries"],
    "properties": {
        "schema_version": {"const": 1, "type": "integer"},
        "source_url": {"type": "string", "minLength": 1, "maxLength": 1000},
        "retrieved_on": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "entries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100000,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "description", "strength_form"],
                "properties": {
                    name: {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": limit,
                        "allOf": [
                            {"pattern": r"\S"},
                            {"pattern": r"^[^\u0000-\u001f\u007f]+$"},
                        ],
                    }
                    for name, limit in (
                        ("code", 80),
                        ("description", 240),
                        ("strength_form", 160),
                    )
                },
            },
        },
    },
}


def load_catalog_snapshot(*, source: Path, checksum: str) -> MedicationCatalogImport:
    """Load or refresh one immutable snapshot after all boundary checks succeed."""
    assert_owner_database_role()
    if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        reason = "catalog_checksum_invalid"
        raise CommandError(reason)
    try:
        with source.open("rb") as stream:
            content = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        reason = "catalog_source_unreadable"
        raise CommandError(reason) from None
    if len(content) > MAX_SOURCE_BYTES or sha256(content).hexdigest() != checksum:
        reason = "catalog_checksum_invalid"
        raise CommandError(reason)
    try:
        raw = json.loads(content)
        validator: SchemaValidator = import_module("jsonschema").Draft202012Validator(
            SOURCE_SCHEMA
        )
        if not validator.is_valid(raw):
            reason = "catalog_schema_invalid"
            raise CommandError(reason)
        retrieved_on = date.fromisoformat(raw["retrieved_on"])
        url = urlsplit(raw["source_url"])
        valid_origin = url.hostname in {
            "anvisa.gov.br",
            "www.anvisa.gov.br",
            "dados.anvisa.gov.br",
        } or (url.hostname == "www.gov.br" and url.path.startswith("/anvisa/"))
        if (
            not valid_origin
            or url.scheme != "https"
            or url.username
            or url.password
            or url.port not in {None, 443}
            or url.fragment
            or retrieved_on > timezone.localdate()
            or re.search(r"[\x00-\x20\x7f]", raw["source_url"]) is not None
            or len({entry["code"] for entry in raw["entries"]}) != len(raw["entries"])
        ):
            reason = "catalog_source_invalid"
            raise CommandError(reason)
    except (UnicodeError, ValueError):
        # Never reflect file content, row text, source paths or URLs in an error.
        reason = "catalog_schema_invalid"
        raise CommandError(reason) from None
    with transaction.atomic():
        batch, created = MedicationCatalogImport.objects.get_or_create(
            checksum=checksum,
            defaults={"source_url": raw["source_url"], "retrieved_on": retrieved_on},
        )
        if created:
            MedicationCatalogEntry.objects.bulk_create(
                [
                    MedicationCatalogEntry(source=batch, **entry)
                    for entry in raw["entries"]
                ],
                batch_size=500,
            )
        return batch
