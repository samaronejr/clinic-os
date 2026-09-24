"""Task-6 envelope capability: fail-closed AES-256 tenant key boundary."""

# ruff: noqa: S106 - CLINIC_SECRET_BACKEND is a backend name, not a secret

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.core.secrets import (
    FileSecretStore,
    SecretUnavailableError,
    secret_store,
)
from apps.identity.models import Clinic, Organization, UserClinicRole
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import (
    EnvelopeContextError,
    EnvelopeError,
    EnvelopeFormatError,
    EnvelopeUnavailableError,
    decrypt,
    encrypt,
    issue_tenant_key,
    reencrypt,
    rewrap_tenant_keys,
    tenant_key_status,
)
from config.settings.contracts import validate_secret_store_env
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.test import override_settings
from psycopg.errors import InsufficientPrivilege

from otp_test_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from uuid import UUID

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

KEK = "ab" * 32
OTHER_KEK = "cd" * 32
PURPOSE = "readiness-check"
PLAINTEXT = b"synthetic envelope plaintext"


@contextmanager
def _owner_scope(organization_id: UUID, user_id: UUID) -> Iterator[None]:
    """Bind tenant and actor GUCs as the migration owner for admin calls."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(user_id)],
        )
        yield


def _secret_dir(tmp_path: Path, kek: str = KEK) -> Path:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    path = root / "tenant-kek.secret"
    path.write_text(kek)
    path.chmod(0o600)
    return root


@dataclass(frozen=True, slots=True)
class EncryptionOrgs:
    """Two organizations that start with no tenant keys issued."""

    organization_a: UUID
    organization_b: UUID


@pytest.fixture
def encryption_orgs(rbac_graph: RbacGraph) -> EncryptionOrgs:
    """Provision keyless organizations for envelope lifecycle tests.

    ``rbac_graph`` already issues tenant keys for its organizations, so the
    rotation and fail-closed assertions here need orgs that begin with no
    key material at all. Memberships are still required because
    ``tenant_context`` authorizes the user against the organization.
    """
    organization_a = uuid4()
    organization_b = uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        for organization_id, name, user_id in (
            (
                organization_a,
                "Encryption Readiness Org A",
                rbac_graph.physician,
            ),
            (
                organization_b,
                "Encryption Readiness Org B",
                rbac_graph.shared_user,
            ),
        ):
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(organization_id)],
            )
            organization = Organization.objects.create(
                id=organization_id,
                name=name,
                cnpj=f"{int(organization_id) % 10**14:014d}",
            )
            clinic = Clinic.objects.create(
                organization=organization,
                name=f"{name} Clinic",
                crm_uf="SP",
                timezone="America/Sao_Paulo",
            )
            UserClinicRole.objects.create(
                user_id=user_id,
                organization_id=organization_id,
                clinic_id=clinic.pk,
                role=UserClinicRole.Role.PHYSICIAN,
            )
    return EncryptionOrgs(organization_a=organization_a, organization_b=organization_b)


def _issue(organization_id: UUID, user_id: UUID, tmp_path: Path) -> Path:
    root = _secret_dir(tmp_path)
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(root)
        ),
        _owner_scope(organization_id, user_id),
    ):
        assert issue_tenant_key() == 1
    return root


def test_envelope_round_trip_binds_tenant_and_purpose(
    rbac_graph: RbacGraph,
    encryption_orgs: EncryptionOrgs,
    tmp_path: Path,
) -> None:
    root = _issue(encryption_orgs.organization_a, rbac_graph.physician, tmp_path)
    with override_settings(
        CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(root)
    ):
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
        ):
            envelope = encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
            assert envelope[:1] == b"\x01"
            assert decrypt(purpose=PURPOSE, envelope=envelope) == PLAINTEXT
        # Each failure aborts its transaction, so each check gets its own.
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
            pytest.raises(EnvelopeFormatError),
        ):
            decrypt(purpose="other-purpose", envelope=envelope)
        tampered = envelope[:-1] + bytes([envelope[-1] ^ 0x01])
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
            pytest.raises(EnvelopeError),
        ):
            decrypt(purpose=PURPOSE, envelope=tampered)
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
            pytest.raises(EnvelopeFormatError),
        ):
            decrypt(purpose=PURPOSE, envelope=b"\x02" + envelope[1:])
        # A tenant without a key can never unwrap another tenant's envelope.
        with (
            runtime_role(),
            tenant_context(rbac_graph.shared_user, encryption_orgs.organization_b),
            pytest.raises(EnvelopeUnavailableError),
        ):
            decrypt(purpose=PURPOSE, envelope=envelope)


def test_key_rotation_retires_and_reencrypts(
    rbac_graph: RbacGraph,
    encryption_orgs: EncryptionOrgs,
    tmp_path: Path,
) -> None:
    root = _issue(encryption_orgs.organization_a, rbac_graph.physician, tmp_path)
    with override_settings(
        CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(root)
    ):
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
        ):
            first = encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
        with _owner_scope(encryption_orgs.organization_a, rbac_graph.physician):
            assert issue_tenant_key() == 2
            statuses = tenant_key_status()
            assert [(row.key_version, row.status) for row in statuses] == [
                (1, "retired"),
                (2, "active"),
            ]
            assert statuses[0].retired_at is not None
            rotated = reencrypt(purpose=PURPOSE, envelope=first)
            assert rotated != first
            assert decrypt(purpose=PURPOSE, envelope=rotated) == PLAINTEXT
            assert decrypt(purpose=PURPOSE, envelope=first) == PLAINTEXT


def test_kek_rewrap_moves_all_versions(
    rbac_graph: RbacGraph,
    encryption_orgs: EncryptionOrgs,
    tmp_path: Path,
) -> None:
    old_root = _issue(encryption_orgs.organization_a, rbac_graph.physician, tmp_path)
    new_root = tmp_path / "next-secrets"
    new_root.mkdir(mode=0o700)
    new_kek = new_root / "tenant-kek.secret"
    new_kek.write_text(OTHER_KEK)
    new_kek.chmod(0o600)
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(old_root)
        ),
        _owner_scope(encryption_orgs.organization_a, rbac_graph.physician),
    ):
        envelope = encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
        assert rewrap_tenant_keys(new_kek=OTHER_KEK) == 1
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(new_root)
        ),
        _owner_scope(encryption_orgs.organization_a, rbac_graph.physician),
    ):
        assert decrypt(purpose=PURPOSE, envelope=envelope) == PLAINTEXT
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(old_root)
        ),
        _owner_scope(encryption_orgs.organization_a, rbac_graph.physician),
        pytest.raises(EnvelopeError),
    ):
        decrypt(purpose=PURPOSE, envelope=envelope)


def test_access_fails_closed_without_backend_or_key(
    rbac_graph: RbacGraph,
    encryption_orgs: EncryptionOrgs,
    tmp_path: Path,
) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
        override_settings(CLINIC_SECRET_BACKEND=None, CLINIC_SECRET_DIR=None),
        pytest.raises(SecretUnavailableError),
    ):
        encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
    root = _secret_dir(tmp_path)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(root)
        ),
        pytest.raises(EnvelopeUnavailableError),
    ):
        encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(root)
        ),
        pytest.raises(EnvelopeUnavailableError),
    ):
        decrypt(purpose=PURPOSE, envelope=b"\x01\x00\x00\x00\x01" + b"x" * 32)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, encryption_orgs.organization_a),
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(root)
        ),
        pytest.raises(EnvelopeContextError),
    ):
        encrypt(purpose="bad\x00purpose", plaintext=PLAINTEXT)


def test_wrong_kek_and_malformed_envelope_fail_closed(
    rbac_graph: RbacGraph,
    encryption_orgs: EncryptionOrgs,
    tmp_path: Path,
) -> None:
    good = _issue(encryption_orgs.organization_a, rbac_graph.physician, tmp_path)
    bad = tmp_path / "bad-secrets"
    bad.mkdir(mode=0o700)
    bad_kek = bad / "tenant-kek.secret"
    bad_kek.write_text(OTHER_KEK)
    bad_kek.chmod(0o600)
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(good)
        ),
        _owner_scope(encryption_orgs.organization_a, rbac_graph.physician),
    ):
        envelope = encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(bad)
        ),
        _owner_scope(encryption_orgs.organization_a, rbac_graph.physician),
        pytest.raises(EnvelopeError),
    ):
        decrypt(purpose=PURPOSE, envelope=envelope)
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(good)
        ),
        _owner_scope(encryption_orgs.organization_a, rbac_graph.physician),
        pytest.raises(EnvelopeFormatError),
    ):
        decrypt(purpose=PURPOSE, envelope=b"short")
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(good)
        ),
        _owner_scope(encryption_orgs.organization_a, rbac_graph.physician),
        pytest.raises(EnvelopeFormatError),
    ):
        decrypt(purpose=PURPOSE, envelope=b"\x02" + b"\x00" * 64)


def test_runtime_role_reaches_only_encrypt_and_decrypt(
    rbac_graph: RbacGraph,
    encryption_orgs: EncryptionOrgs,
    tmp_path: Path,
    app_database_url: str,
) -> None:
    _issue(encryption_orgs.organization_a, rbac_graph.physician, tmp_path)
    with psycopg.connect(app_database_url) as app:
        app.execute(
            "SELECT set_config('app.current_tenant', %s, false)",
            [str(encryption_orgs.organization_a)],
        )
        denied: tuple[tuple[str, list[str]], ...] = (
            ("SELECT * FROM clinic_app.tenancy_tenantdatakey", []),
            (
                "INSERT INTO clinic_app.tenancy_tenantdatakey ("
                "id, organization_id, key_version, wrapped_key, status, "
                "created_at) VALUES (gen_random_uuid(), %s, 9, '\\x00', "
                "'active', now())",
                [str(encryption_orgs.organization_a)],
            ),
            ("SELECT clinic_app.tenant_dek_issue(%s)", [KEK]),
            ("SELECT clinic_app.tenant_dek_unwrap(%s, 1)", [KEK]),
            ("SELECT clinic_app.tenant_dek_rewrap(%s, %s)", [KEK, OTHER_KEK]),
            (
                "SELECT clinic_app.tenant_reencrypt(%s, %s, '\\x00')",
                [KEK, PURPOSE],
            ),
            ("SELECT * FROM clinic_app.tenant_key_status()", []),
        )
        for statement, params in denied:
            with pytest.raises(InsufficientPrivilege):
                app.execute(statement, params)
            app.rollback()
        # set_config is transactional: the denied-statement rollbacks above
        # discarded it, so re-establish the tenant before the allowed calls.
        app.execute(
            "SELECT set_config('app.current_tenant', %s, false)",
            [str(encryption_orgs.organization_a)],
        )
        envelope = app.execute(
            "SELECT clinic_app.tenant_encrypt(%s, %s, %s)",
            [KEK, PURPOSE, PLAINTEXT],
        ).fetchone()
        assert envelope is not None
        opened = app.execute(
            "SELECT clinic_app.tenant_decrypt(%s, %s, %s)",
            [KEK, PURPOSE, bytes(envelope[0])],
        ).fetchone()
        assert opened == (PLAINTEXT,)


def test_key_table_posture_is_owner_only_force_rls(rbac_graph: RbacGraph) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT class.relrowsecurity, class.relforcerowsecurity, "
            "class.relowner::regrole::text "
            "FROM pg_catalog.pg_class AS class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = 'clinic_app' "
            "AND class.relname = 'tenancy_tenantdatakey'"
        )
        assert cursor.fetchone() == (True, True, "clinic_owner")
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema = 'clinic_app' "
            "AND table_name = 'tenancy_tenantdatakey' "
            "AND grantee IN ('clinic_app', 'clinic_resolver', 'PUBLIC')"
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT procedure.proname, procedure.proowner::regrole::text, "
            "procedure.prosecdef "
            "FROM pg_catalog.pg_proc AS procedure "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = procedure.pronamespace "
            "WHERE namespace.nspname = 'clinic_app' "
            "AND procedure.proname LIKE 'tenant_%'"
        )
        functions = cursor.fetchall()
    expected = {
        "tenant_dek_issue",
        "tenant_dek_rewrap",
        "tenant_dek_unwrap",
        "tenant_decrypt",
        "tenant_encrypt",
        "tenant_key_status",
        "tenant_reencrypt",
    }
    assert {name for name, _, _ in functions} == expected
    assert all(owner == "clinic_owner" and secdef for _, owner, secdef in functions)


def test_secret_store_contract_is_closed(tmp_path: Path) -> None:
    root = _secret_dir(tmp_path)
    store = FileSecretStore(str(root))
    assert store.get_secret("tenant-kek") == KEK
    with pytest.raises(SecretUnavailableError):
        store.get_secret("missing")
    with pytest.raises(SecretUnavailableError):
        store.get_secret("../escape")
    with pytest.raises(SecretUnavailableError):
        store.get_secret("UPPER")
    loose = root / "loose.secret"
    loose.write_text(KEK)
    loose.chmod(0o644)
    with pytest.raises(SecretUnavailableError):
        store.get_secret("loose")
    bad = root / "bad.secret"
    bad.write_text("not-hex")
    bad.chmod(0o600)
    with pytest.raises(SecretUnavailableError):
        store.get_secret("bad")
    link = root / "link.secret"
    link.symlink_to(root / "tenant-kek.secret")
    with pytest.raises(SecretUnavailableError):
        store.get_secret("link")
    with pytest.raises(SecretUnavailableError):
        FileSecretStore(str(tmp_path / "absent")).get_secret("tenant-kek")


def test_secret_store_factory_has_no_default_or_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLINIC_SECRET_BACKEND", raising=False)
    monkeypatch.delenv("CLINIC_SECRET_DIR", raising=False)
    with (
        override_settings(CLINIC_SECRET_BACKEND=None, CLINIC_SECRET_DIR=None),
        pytest.raises(SecretUnavailableError),
    ):
        secret_store()
    root = _secret_dir(tmp_path)
    with override_settings(
        CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=str(root)
    ):
        assert secret_store().get_secret("tenant-kek") == KEK
    monkeypatch.delenv("CLINIC_SECRET_DIR", raising=False)
    with (
        override_settings(
            CLINIC_SECRET_BACKEND="synthetic-file", CLINIC_SECRET_DIR=None
        ),
        pytest.raises(SecretUnavailableError),
    ):
        secret_store()


def test_secret_store_env_contract_rejects_partial_configuration() -> None:
    assert validate_secret_store_env(None, None) == (None, None)
    assert validate_secret_store_env("synthetic-file", "/srv/secrets") == (
        "synthetic-file",
        "/srv/secrets",
    )
    with pytest.raises(ImproperlyConfigured):
        validate_secret_store_env(None, "/srv/secrets")
    with pytest.raises(ImproperlyConfigured):
        validate_secret_store_env("plaintext-env", "/srv/secrets")
    with pytest.raises(ImproperlyConfigured):
        validate_secret_store_env("synthetic-file", None)
    with pytest.raises(ImproperlyConfigured):
        validate_secret_store_env("synthetic-file", "")
