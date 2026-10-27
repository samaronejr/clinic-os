"""Recreate immutable migration references after transactional test flushes."""

from __future__ import annotations

from importlib import import_module

from django.db import connection


def seed_class_policies() -> None:
    """Reuse the migration's exact seed without changing existing policy rows."""
    migration = import_module(
        "apps.prescription.migrations.0013_class_catalog_directions"
    )
    statement = migration.SEED_SQL.rstrip().removesuffix(";")
    with connection.cursor() as cursor:
        cursor.execute(f"{statement} ON CONFLICT (category) DO NOTHING")
