"""Exercise restore command wiring with no database or container execution."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

from apps.retention.storage import SyntheticPurgeStorage
from apps.scribe.storage import media_location
from django.test import override_settings
from ops.testing import renewal_runner, restore_rehearsal, restore_rehearsal_acceptance
from ops.testing.ci_postgres_runtime import CiDatabaseLease
from ops.testing.restore_container import ContainerPostgres, VersionEvidence
from ops.testing.restore_contract import REQUIRED_EMPTY, SourceScope, expected_toc
from ops.testing.restore_purge import CURRENT_TOMBSTONES_SQL
from ops.testing.restore_queries import EQUALITY_RELATIONS

if TYPE_CHECKING:
    import pytest


def test_restore_cli_passes_the_explicit_media_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "media"
    monkeypatch.setattr(
        "sys.argv",
        [
            "restore-rehearsal",
            "--source-container",
            "a" * 64,
            "--target-container",
            "b" * 64,
            "--source-database",
            "synthetic_source",
            "--target-database",
            "synthetic_target",
            "--credentials-fd",
            "3",
            "--work-dir",
            str(tmp_path),
            "--evidence",
            str(tmp_path / "evidence.json"),
            "--probe",
            str(tmp_path / "probe.json"),
            "--object-store",
            str(tmp_path / "attachments"),
            "--media-store",
            str(media),
            "--source-media-store",
            str(tmp_path / "media-source"),
            "--source-claim",
            str(uuid4()),
            "--target-claim",
            str(uuid4()),
        ],
    )
    credentials = dict.fromkeys(
        ("source_password", "target_password", "target_app_password", "target_app_url"),
        "synthetic",
    )
    monkeypatch.setattr(
        "ops.testing.restore_rehearsal._read_credentials",
        Mock(return_value=credentials),
    )
    monkeypatch.setattr("ops.testing.restore_rehearsal._read_probe", Mock())
    run = Mock()
    monkeypatch.setattr("ops.testing.restore_rehearsal.run_rehearsal", run)
    monkeypatch.setattr(
        "ops.testing.restore_rehearsal._evidence_json", Mock(return_value={})
    )
    monkeypatch.setattr("ops.testing.restore_rehearsal._publish_evidence", Mock())

    assert restore_rehearsal.main() == 0

    run.assert_called_once()
    assert run.call_args.kwargs["media_store"] == media
    assert run.call_args.kwargs["source_media_store"] == tmp_path / "media-source"


def test_acceptance_caller_and_make_recipe_forward_an_isolated_media_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = "synthetic"
    lease = CiDatabaseLease(
        app_password=credential,
        ca_path=tmp_path / "ca.pem",
        claim_id=str(uuid4()),
        container_id="a" * 64,
        database="synthetic",
        host_port=15432,
        owner_password=credential,
        postgres_password=credential,
        super_password=credential,
    )
    observed: list[Path] = []

    def run(
        argv: tuple[str, ...],
        environment: dict[str, str],
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        assert argv[-1] == "restore-rehearsal"
        media = Path(environment["RESTORE_MEDIA_STORE"])
        assert media.is_absolute()
        assert media.is_relative_to(tmp_path)
        assert media.is_dir()
        assert stat.S_IMODE(media.stat().st_mode) == 0o700
        log = tmp_path / "make-argv.log"
        # Run the real recipe, substituting printf for uv so no Python, DB,
        # Docker or server command can run.
        assert (
            renewal_runner._run_bounded(
                [
                    "/usr/bin/make",
                    "--no-print-directory",
                    "UV=/usr/bin/printf '%s\\n'",
                    "restore-rehearsal",
                ],
                environment,
                log,
                timeout=10,
                cwd=restore_rehearsal_acceptance.PROJECT_ROOT,
            )
            == 0
        ), log.read_text()
        arguments = log.read_text().splitlines()
        assert arguments[arguments.index("--media-store") + 1] == str(media)
        observed.append(media)
        Path(environment["RESTORE_EVIDENCE_PATH"]).write_bytes(b"synthetic evidence")

    monkeypatch.setattr("ops.testing.restore_rehearsal_acceptance._run", run)
    assert (
        restore_rehearsal_acceptance._run_restore(
            lease,
            lease,
            tmp_path,
            tmp_path / "probe.json",
            tmp_path / "attachments",
            tmp_path / "secrets",
        )
        == b"synthetic evidence"
    )
    assert len(observed) == 1


def test_seed_uses_the_acceptance_source_media_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = "synthetic"
    lease = Mock(spec=CiDatabaseLease, super_password=credential)
    monkeypatch.setattr("ops.testing.restore_rehearsal_acceptance.database_url", Mock())
    run = Mock()
    monkeypatch.setattr("ops.testing.restore_rehearsal_acceptance._run", run)

    restore_rehearsal_acceptance._seed(
        lease, tmp_path, tmp_path / "secrets", tmp_path / "attachments"
    )

    environment = run.call_args.args[1]
    assert environment["SCRIBE_MEDIA_ROOT"] == str(tmp_path / "media-source")


def _stub_rehearsal_database_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only database/container/process boundaries are inert. The acceptance
    # caller, Makefile, parser, rehearsal, copy, namespace check and purge run.
    monkeypatch.setattr(restore_rehearsal, "require_lease_claim", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_hba", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_equal_target_seed", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_empty_target", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_owner_rls_acl_posture", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_audit_chain", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_key_probe", Mock())
    monkeypatch.setattr(restore_rehearsal, "_probe_restored_app_role", Mock())
    monkeypatch.setattr(restore_rehearsal, "_run_verify", Mock())
    monkeypatch.setattr(
        restore_rehearsal,
        "source_scope",
        Mock(
            return_value=SourceScope(
                active_writer_count=0,
                empty_relation_counts=dict.fromkeys(REQUIRED_EMPTY, 0),
                foreign_audit_organization_count=0,
                identity_user_count=4,
                organization_count=1,
                tenant_data_key_count=1,
                totp_without_identity_count=0,
                unexpected_domain_relations=(),
                users_without_role_count=0,
            )
        ),
    )
    monkeypatch.setattr(
        restore_rehearsal,
        "migration_leaves",
        Mock(return_value="synthetic migration leaves"),
    )
    monkeypatch.setattr(
        restore_rehearsal,
        "relation_fingerprints",
        Mock(return_value=dict.fromkeys(EQUALITY_RELATIONS, "a" * 32)),
    )
    monkeypatch.setattr(
        restore_rehearsal, "require_audit_content", Mock(return_value=0)
    )
    monkeypatch.setattr(
        restore_rehearsal,
        "require_equal_tenant_key_status",
        Mock(return_value="synthetic key status"),
    )
    monkeypatch.setattr(restore_rehearsal, "verify_object_store", Mock(return_value=0))
    monkeypatch.setattr(
        restore_rehearsal, "require_sequence_headroom", Mock(return_value=())
    )


def test_acceptance_replays_source_tombstones_into_its_fresh_media_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "c" * 64
    source_storage = SyntheticPurgeStorage(tmp_path / "media-source")
    source_storage.put(key, b"synthetic ciphertext")
    source_storage.mark_pending(key, str(uuid4()))
    with override_settings(
        SCRIBE_DEV_MINIO=False, SCRIBE_MEDIA_ROOT=str(tmp_path / "media-source")
    ):
        namespace = media_location()
    source = Mock(spec=ContainerPostgres, container_id="a" * 64, database="source")
    target = Mock(spec=ContainerPostgres, container_id="b" * 64, database="target")
    source.sql.return_value = json.dumps(
        [{"objects": [{"key": key, "location": namespace}]}]
    )
    target.sql.return_value = json.dumps([{"key": key, "location": namespace}])
    source.dump.return_value = b"synthetic logical archive"
    source.list_archive.return_value = target.list_archive.return_value = "\n".join(
        f"{index}; 0 0 {entry.replace('clinic_app.', 'clinic_app ')} clinic_owner"
        for index, entry in enumerate(expected_toc(), 1)
    )
    source.require_postgresql_16_14.return_value = (
        target.require_postgresql_16_14.return_value
    ) = VersionEvidence("16.14", "16.14", "16.14")
    monkeypatch.setattr(
        "ops.testing.restore_rehearsal.ContainerPostgres",
        Mock(side_effect=[source, target]),
    )
    _stub_rehearsal_database_boundaries(monkeypatch)
    probe = tmp_path / "probe.json"
    probe.write_text(
        json.dumps(
            dict.fromkeys(
                ("organization_id", "kek", "envelope_hex", "expected_sha256"),
                "synthetic",
            )
        )
    )
    probe.chmod(0o600)
    deleted: list[str] = []
    original_delete = SyntheticPurgeStorage.delete

    def delete(storage: SyntheticPurgeStorage, object_key: str) -> None:
        assert storage._root == tmp_path / "media-restored"
        assert storage.get(object_key) == b"synthetic ciphertext"
        deleted.append(object_key)
        original_delete(storage, object_key)

    monkeypatch.setattr("apps.retention.storage.SyntheticPurgeStorage.delete", delete)

    def run(
        argv: tuple[str, ...],
        environment: dict[str, str],
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        log = tmp_path / "acceptance-argv.log"
        assert (
            renewal_runner._run_bounded(
                [
                    argv[0],
                    "--no-print-directory",
                    "UV=/usr/bin/printf '%s\\n'",
                    argv[-1],
                ],
                environment,
                log,
                timeout=10,
                cwd=restore_rehearsal_acceptance.PROJECT_ROOT,
            )
            == 0
        ), log.read_text()
        arguments = log.read_text().splitlines()
        monkeypatch.setattr(
            "sys.argv",
            [
                "restore-rehearsal",
                *arguments[arguments.index("ops.testing.restore_rehearsal") + 1 :],
            ],
        )
        assert restore_rehearsal.main() == 0

    monkeypatch.setattr("ops.testing.restore_rehearsal_acceptance._run", run)
    credential = "synthetic"
    lease = Mock(
        spec=CiDatabaseLease,
        super_password=credential,
        app_password=credential,
        claim_id=str(uuid4()),
        container_id="a" * 64,
        database="synthetic",
    )
    target_lease = Mock(
        spec=CiDatabaseLease,
        super_password=credential,
        app_password=credential,
        claim_id=str(uuid4()),
        container_id="b" * 64,
        database="synthetic_target",
    )
    monkeypatch.setattr(
        "ops.testing.restore_rehearsal_acceptance.database_url",
        Mock(return_value="synthetic"),
    )
    evidence = json.loads(
        restore_rehearsal_acceptance._run_restore(
            lease,
            target_lease,
            tmp_path,
            probe,
            tmp_path / "attachments",
            tmp_path / "secrets",
        )
    )

    assert deleted == [key]
    assert SyntheticPurgeStorage(tmp_path / "media-restored").absent(key)
    assert source_storage.get(key) == b"synthetic ciphertext"
    assert len(source_storage.pending_uploads()) == 1
    assert source.sql.call_args.args == (CURRENT_TOMBSTONES_SQL,)
    target.restore.assert_called_once_with(b"synthetic logical archive")
    assert evidence["archive_absent"] is True
