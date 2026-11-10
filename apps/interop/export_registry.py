"""Export v1 datasets: the frozen relation set described by live model metadata.

Each exported relation becomes one dataset with a JSON Lines file and a CSV file.
Encrypted columns are decrypted with their field's envelope purpose and codec;
other binary columns are base64. ``EXCLUDED`` names every relation left out and
why, so the union always equals the recovery manifest's domain relations.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Final

from django.apps import apps as django_apps
from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.interop.migrations._export_sql import EXCLUDED_V1, EXPORT_V1
from apps.tenancy.fields import (
    EncryptedBytesField,
    EncryptedDateField,
    EncryptedField,
    EncryptedJSONField,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

FORMAT: Final = "clinic-organization-export-v1"
EXCLUSION_REASONS: Final = {
    "credential": "authentication secrets, sessions and device material",
    "platform": "platform-wide records without a tenant owner",
    "integrity_ledger": "hash-chained audit ledger, verified by its own procedure",
    "machinery": "delivery, retry, projection and receipt machinery",
    "presentation": "per-user interface preferences",
    "discarded": "content a clinician explicitly discarded",
    "transient_media": "raw audio retained only until its purge policy",
    "derived_archive": "the encrypted bytes of export archives themselves",
}
EXCLUDED: Final[Mapping[str, str]] = EXCLUDED_V1
_TYPES: Final[tuple[tuple[type[models.Field[object, object]], str], ...]] = (
    (models.BooleanField, "boolean"),
    (models.UUIDField, "uuid"),
    (models.DateTimeField, "timestamp"),
    (models.DateField, "date"),
    (models.TimeField, "time"),
    (models.DurationField, "interval"),
    (models.DecimalField, "decimal"),
    (models.FloatField, "number"),
    (models.IntegerField, "integer"),
    (models.JSONField, "json"),
    (models.BinaryField, "base64"),
)


@dataclass(frozen=True, slots=True)
class Column:
    """One exported column: its wire type and, when encrypted, its purpose."""

    name: str
    type: str
    purpose: str | None = None


@dataclass(frozen=True, slots=True)
class Dataset:
    """One exported relation and the columns its model declares."""

    relation: str
    domain: str
    key: str
    columns: tuple[Column, ...]


def _column(field: models.Field[object, object]) -> Column:
    name = str(field.column)
    if isinstance(field, EncryptedField):
        kind = (
            "date"
            if isinstance(field, EncryptedDateField)
            else "json"
            if isinstance(field, EncryptedJSONField)
            else "base64"
            if isinstance(field, EncryptedBytesField)
            else "text"
        )
        return Column(name, kind, field.envelope_purpose)
    if isinstance(field, ArrayField):
        return Column(name, "array")
    target = field.target_field if isinstance(field, models.ForeignKey) else field
    for field_type, wire_type in _TYPES:
        if isinstance(target, field_type):
            return Column(name, wire_type)
    return Column(name, "text")


@cache
def datasets() -> tuple[Dataset, ...]:
    """Return every export v1 dataset in archive order."""
    by_table = {
        model._meta.db_table: model  # noqa: SLF001 - Django model metadata API
        for model in django_apps.get_models(include_auto_created=True)
    }
    result: list[Dataset] = []
    for relation, key, _kind, omitted, _scope in EXPORT_V1:
        model = by_table.get(relation)
        columns = (
            ()
            if model is None
            else tuple(
                sorted(
                    (
                        _column(field)
                        for field in model._meta.concrete_fields  # noqa: SLF001
                        if field.column not in omitted
                    ),
                    key=lambda column: column.name,
                )
            )
        )
        if relation == "identity_user":
            columns = tuple(
                column
                for column in columns
                if column.name
                in {
                    "id",
                    "username",
                    "first_name",
                    "last_name",
                    "email",
                    "is_active",
                    "date_joined",
                    "last_login",
                }
            )
        result.append(Dataset(relation, relation.split("_", 1)[0], key, columns))
    return tuple(result)
