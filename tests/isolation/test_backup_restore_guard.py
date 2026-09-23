from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from ops.testing import (
    restore_container,
    restore_contract,
    restore_transport,
    restore_verification,
    tls_materializer,
)
from ops.testing.restore_queries import EQUALITY_RELATIONS
from ops.testing.tls_contract import POSTGRES_IMAGE

if TYPE_CHECKING:
    from pathlib import Path

SYNTHETIC_CREDENTIAL = "synthetic-private-password"


class _SuccessfulProcess:
    async def wait(self) -> int:
        return 0


def test_fixed_manifest_and_postgresql_commands_are_closed() -> None:
    assert restore_contract.TABLE_DATA == (
        "clinic_app.audit_event",
        "clinic_app.identity_clinic",
        "clinic_app.identity_organization",
        "clinic_app.identity_user",
        "clinic_app.identity_userclinicrole",
        "clinic_app.intake_patient",
        "clinic_app.intake_patientclinicenrollment",
        "clinic_app.otp_totp_totpdevice",
        "clinic_app.scheduling_appointment",
        "clinic_app.scheduling_availabilityblock",
    )
    assert restore_contract.SEQUENCE_SET == (
        "clinic_app.audit_event_seq_seq",
        "clinic_app.otp_totp_totpdevice_id_seq",
    )
    dump = restore_contract.dump_argv("clinic_source")
    assert dump[:5] == (
        "pg_dump",
        "--data-only",
        "--format=custom",
        "--column-inserts",
        "--strict-names",
    )
    assert "--no-owner" not in dump
    assert dump[-1] == "clinic_source"
    restore = restore_contract.restore_argv("clinic_restore")
    assert restore[:9] == (
        "pg_restore",
        "--schema=clinic_app",
        "--strict-names",
        "--data-only",
        "--no-owner",
        "--no-acl",
        "--single-transaction",
        "--exit-on-error",
        "--dbname=clinic_restore",
    )
    assert all(
        "clinic_app." not in item for item in restore if item.startswith("--table=")
    )


def test_materializer_prefers_the_cached_pinned_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    async def create_process(*argv: str, **_options: object) -> _SuccessfulProcess:
        commands.append(argv)
        return _SuccessfulProcess()

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_process,
    )

    assert asyncio.run(tls_materializer._pull_postgres()) == 0
    assert commands == [
        (
            "/usr/bin/docker",
            "image",
            "inspect",
            POSTGRES_IMAGE,
        )
    ]


def test_toc_and_source_scope_reject_before_restore() -> None:
    toc = """;
1; 0 0 TABLE DATA clinic_app identity_user clinic_owner
2; 0 0 SEQUENCE SET clinic_app audit_event_seq_seq clinic_owner
"""
    assert restore_contract.normalize_toc(toc) == (
        "SEQUENCE SET clinic_app.audit_event_seq_seq",
        "TABLE DATA clinic_app.identity_user",
    )
    with pytest.raises(restore_contract.RestoreContractError, match="manifest"):
        restore_contract.require_exact_toc(toc)

    clean = restore_contract.SourceScope(
        active_writer_count=0,
        empty_relation_counts=dict.fromkeys(restore_contract.REQUIRED_EMPTY, 0),
        foreign_audit_organization_count=0,
        identity_user_count=4,
        organization_count=1,
        totp_without_identity_count=0,
        unexpected_domain_relations=(),
        users_without_role_count=0,
    )
    restore_contract.require_source_scope(clean)
    with pytest.raises(restore_contract.RestoreContractError, match="organization"):
        restore_contract.require_source_scope(replace(clean, organization_count=2))
    with pytest.raises(restore_contract.RestoreContractError, match="empty"):
        restore_contract.require_source_scope(
            replace(
                clean,
                empty_relation_counts={
                    **clean.empty_relation_counts,
                    "tenancy_tenantprobe": 1,
                },
            )
        )


def test_archive_is_private_hashed_and_checked_before_target_input(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "recovery.dump"
    sidecar = tmp_path / "recovery.dump.sha256"

    restore_transport.write_private_archive(archive, b"synthetic-custom-archive")
    restore_transport.write_hash_sidecar(archive, sidecar)

    assert archive.stat().st_mode & 0o777 == 0o600
    assert sidecar.stat().st_mode & 0o777 == 0o600
    restore_transport.verify_hash_sidecar(archive, sidecar)
    archive.write_bytes(b"corrupt")
    with pytest.raises(restore_contract.RestoreContractError, match="SHA-256"):
        restore_transport.verify_hash_sidecar(archive, sidecar)


def test_postgresql_clients_run_only_inside_the_exact_container() -> None:
    calls: list[tuple[tuple[str, ...], bytes | None, dict[str, str]]] = []
    responses = iter(
        (
            b"pg_dump (PostgreSQL) 16.14\n",
            b"pg_restore (PostgreSQL) 16.14\n",
            b"160014\n",
        )
    )

    def run(
        argv: tuple[str, ...], standard_input: bytes | None, environment: dict[str, str]
    ) -> bytes:
        calls.append((argv, standard_input, environment))
        return next(responses)

    client = restore_container.ContainerPostgres(
        container_id="a" * 64,
        database="clinic_source",
        password=SYNTHETIC_CREDENTIAL,
        runner=run,
    )

    assert client.require_postgresql_16_14() == restore_container.VersionEvidence(
        dump_client="16.14",
        restore_client="16.14",
        server="16.14",
    )
    assert all(
        call[0][:5] == ("/usr/bin/docker", "exec", "-i", "--env", "PGPASSWORD")
        for call in calls
    )
    assert all(call[0][5] == "a" * 64 for call in calls)
    assert all(
        call[2]
        == {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PGPASSWORD": SYNTHETIC_CREDENTIAL,
            "TZ": "UTC",
        }
        for call in calls
    )
    assert all(
        SYNTHETIC_CREDENTIAL not in argument for call in calls for argument in call[0]
    )


def test_target_migrations_and_read_only_fingerprints_must_equal_source() -> None:
    source_leaves = "audit|0005\nidentity|0005\nscheduling|0002\n"
    restore_verification.require_equal_migration_leaves(source_leaves, source_leaves)
    with pytest.raises(restore_contract.RestoreContractError, match="migration leaf"):
        restore_verification.require_equal_migration_leaves(
            source_leaves,
            "audit|0005\nidentity|0005\n",
        )

    source = dict.fromkeys(EQUALITY_RELATIONS, "a" * 32)
    restore_verification.require_equal_fingerprints(source, dict(source))
    with pytest.raises(
        restore_contract.RestoreContractError, match="read-only equality"
    ):
        restore_verification.require_equal_fingerprints(
            source,
            {**source, "identity_user": "b" * 32},
        )
