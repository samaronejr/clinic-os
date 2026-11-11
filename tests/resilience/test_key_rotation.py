"""Todo 72: tenant key rotation drill on the real pgcrypto envelope boundary."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.tenancy.envelope import (
    EnvelopeError,
    blind_indexes,
    decrypt,
    encrypt,
    issue_tenant_key,
    reencrypt,
    rewrap_tenant_keys,
    tenant_key_status,
)
from django.db import connection, transaction

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from uuid import UUID

    from conftest import TenantGraph

pytestmark = pytest.mark.django_db(transaction=True)
PURPOSE = "resilience.key-rotation-probe"
PLAINTEXT = b"synthetic key rotation probe"
TERM = b"synthetic-index-term"


@contextmanager
def tenant_scope(organization_id: UUID) -> Iterator[None]:
    """Bind the tenant GUC for owner-side key administration, like provisioning."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        yield


def store_kek(root: Path, value: str) -> None:
    """Replace the configured synthetic KEK, as an operator switch would."""
    path = root / "tenant-kek.secret"
    path.write_text(value)
    path.chmod(0o600)


def wrapped_digests(superuser_database_url: str) -> list[tuple[str, int, str]]:
    with psycopg.connect(superuser_database_url) as raw:
        rows = raw.execute(
            "SELECT organization_id::text, key_version, "
            "encode(clinic_app.digest(wrapped_key, 'sha256'), 'hex') "
            "FROM clinic_app.tenancy_tenantdatakey ORDER BY 1, 2"
        ).fetchall()
    return [(str(org), int(version), str(digest)) for org, version, digest in rows]


def test_kek_rotation_must_rewrap_every_tenant_before_the_store_switches(
    tenant_graph: TenantGraph, synthetic_secret_backend: Path
) -> None:
    tenants = (tenant_graph.organization_a, tenant_graph.organization_b)
    old = (synthetic_secret_backend / "tenant-kek.secret").read_text()
    new = secrets.token_hex(32)
    envelopes: dict[UUID, bytes] = {}
    indexes: dict[UUID, tuple[bytes, ...]] = {}
    for tenant in tenants:
        with tenant_scope(tenant):
            envelopes[tenant] = encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
            indexes[tenant] = blind_indexes(purpose=PURPOSE, plaintext=TERM)
    # A partial rotation strands every tenant that was not rewrapped.
    with tenant_scope(tenant_graph.organization_a):
        assert rewrap_tenant_keys(new_kek=new) == 1
    store_kek(synthetic_secret_backend, new)
    with tenant_scope(tenant_graph.organization_a):
        assert decrypt(purpose=PURPOSE, envelope=envelopes[tenants[0]]) == PLAINTEXT
    with pytest.raises(EnvelopeError), tenant_scope(tenant_graph.organization_b):
        decrypt(purpose=PURPOSE, envelope=envelopes[tenants[1]])
    # The runbook order: every tenant under the old KEK first, then the switch.
    store_kek(synthetic_secret_backend, old)
    with tenant_scope(tenant_graph.organization_b):
        assert rewrap_tenant_keys(new_kek=new) == 1
    store_kek(synthetic_secret_backend, new)
    for tenant in tenants:
        with tenant_scope(tenant):
            assert decrypt(purpose=PURPOSE, envelope=envelopes[tenant]) == PLAINTEXT
            # Rewrapping changes no ciphertext and no blind index.
            assert blind_indexes(purpose=PURPOSE, plaintext=TERM) == indexes[tenant]
    # A stale copy of the old KEK (for example an escrow nobody updated) now
    # opens nothing.
    store_kek(synthetic_secret_backend, old)
    for tenant in tenants:
        with pytest.raises(EnvelopeError), tenant_scope(tenant):
            decrypt(purpose=PURPOSE, envelope=envelopes[tenant])


def test_a_rewrap_under_the_wrong_kek_changes_nothing(
    tenant_graph: TenantGraph,
    synthetic_secret_backend: Path,
    superuser_database_url: str,
) -> None:
    before = wrapped_digests(superuser_database_url)
    store_kek(synthetic_secret_backend, secrets.token_hex(32))
    with pytest.raises(EnvelopeError), tenant_scope(tenant_graph.organization_a):
        rewrap_tenant_keys(new_kek=secrets.token_hex(32))
    assert wrapped_digests(superuser_database_url) == before


def test_dek_rotation_keeps_history_readable_and_reencrypts_forward(
    tenant_graph: TenantGraph,
) -> None:
    tenant = tenant_graph.organization_a
    with tenant_scope(tenant):
        before = encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
        [first_index] = blind_indexes(purpose=PURPOSE, plaintext=TERM)
        assert issue_tenant_key() == 2
    with tenant_scope(tenant):
        assert [(key.key_version, key.status) for key in tenant_key_status()] == [
            (1, "retired"),
            (2, "active"),
        ]
        # Retired versions still open old ciphertext; new writes use version 2.
        assert decrypt(purpose=PURPOSE, envelope=before) == PLAINTEXT
        forward = reencrypt(purpose=PURPOSE, envelope=before)
        assert forward != before
        assert decrypt(purpose=PURPOSE, envelope=forward) == PLAINTEXT
        # Reads match every version's digest, so existing indexes stay reachable.
        assert blind_indexes(purpose=PURPOSE, plaintext=TERM)[0] == first_index
        assert len(blind_indexes(purpose=PURPOSE, plaintext=TERM)) == 2
