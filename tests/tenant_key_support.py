# ruff: noqa: S106 - CLINIC_SECRET_BACKEND is a backend name, not a secret
"""Provision the tenant DEK and KEK fixtures protected fields require."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import pytest
from apps.tenancy.envelope import issue_tenant_key
from django.db import connection, transaction
from django.test import override_settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from uuid import UUID


def issue_tenant_key_for(organization_id: UUID) -> None:
    """Provision the tenant DEK for one test organization as clinic_owner.

    Protected fields cannot be written until the organization holds an
    active wrapped DEK; tests provision it through the same owner-only
    boundary production uses.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        issue_tenant_key()


@pytest.fixture(autouse=True)
def synthetic_secret_backend(tmp_path: Path) -> Iterator[Path]:
    """Configure the synthetic-file secret backend with one tenant KEK.

    Autouse so every test that writes protected fields has key material;
    fail-closed tests still override the settings to ``None`` explicitly.
    """
    root = tmp_path / "tenant-key-secrets"
    root.mkdir(mode=0o700)
    kek = root / "tenant-kek.secret"
    kek.write_text(secrets.token_hex(32))
    kek.chmod(0o600)
    with override_settings(
        CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(root)
    ):
        yield root
