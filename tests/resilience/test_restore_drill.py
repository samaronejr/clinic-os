"""Todo 72, scenario 17: the restore drill needs rows, objects and keys together.

No container or database runs here: the escrow, object-backup and preflight
boundaries are exercised on real files, and SQL observations are stubbed at
the container client exactly where the rehearsal reads them.
"""

from __future__ import annotations

import copy
import json
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.identity.recovery_exercise import latest_recovery_exercise
from ops.testing import (
    restore_escrow,
    restore_rehearsal,
    restore_rehearsal_acceptance,
)
from ops.testing.restore_container import ContainerPostgres, VersionEvidence
from ops.testing.restore_contract import (
    REQUIRED_EMPTY,
    RestoreContractError,
    SourceScope,
    expected_toc,
)
from ops.testing.restore_queries import EQUALITY_RELATIONS

from isolation.test_isolation_normative_schemas import _object_schemas

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject

ROOT: Final = Path(__file__).resolve().parents[2]
SCHEMA: Final = ROOT / "docs/operations/recovery-exercise.schema.json"
ORGANIZATION: Final = "10000000-0000-4000-8000-000000000020"
KEK: Final = "ab" * 32
ENVELOPE: Final = "01" + "00" * 16
DIGEST: Final = "cd" * 32
OBJECT_KEY: Final = "ef" * 32
RESULT_KEY: Final = "orders/" + "12" * 32
MANIFEST: Final = (
    f"{OBJECT_KEY}|{ORGANIZATION}|{'aa' * 32}|10\n"
    f"{RESULT_KEY}|{ORGANIZATION}|{'bb' * 32}|20\n"
)


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def escrow_with(root: Path, value: str = KEK, mode: int = 0o400) -> Path:
    escrow = private_dir(root / "key-escrow")
    secret = escrow / restore_escrow.KEK_SECRET_FILE
    secret.write_text(value)
    secret.chmod(mode)
    return escrow


def backup_with(root: Path, *keys: str) -> Path:
    backup = private_dir(root / "object-backup")
    for key in keys:
        path = backup / key
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(b"synthetic ciphertext")
    return backup


def source_client(*, digest: str = DIGEST, manifest: str = MANIFEST) -> Mock:
    client = Mock(spec=ContainerPostgres, container_id="a" * 64, database="source")

    def sql(statement: str) -> str:
        if "tenant_decrypt" in statement:
            return f"SET\n{digest}\n"
        if "ehr_clinicalattachment" in statement:
            return manifest
        return "[]"

    client.sql.side_effect = sql
    return client


PROBE: Final = restore_rehearsal.KeyProbe(ORGANIZATION, ENVELOPE, DIGEST)


@pytest.mark.parametrize(
    ("arrange", "message"),
    [
        (lambda root: root / "absent", "escrow is missing"),
        (lambda root: private_dir(root / "empty"), "escrow is missing"),
        (lambda root: escrow_with(root, mode=0o644), "escrow is not private"),
        (lambda root: escrow_with(root, value="not-a-key"), "escrow is unreadable"),
    ],
    ids=["no-escrow", "no-kek", "shared-kek", "damaged-kek"],
)
def test_a_missing_or_damaged_kek_escrow_fails_with_an_explicit_message(
    tmp_path: Path, arrange: object, message: str
) -> None:
    escrow = arrange(tmp_path)  # type: ignore[operator]
    with pytest.raises(
        RestoreContractError, match=f"restore drill failed: .*{message}"
    ):
        restore_escrow.read_key_escrow(escrow)


def test_a_symlinked_kek_is_refused_and_a_private_one_is_read(tmp_path: Path) -> None:
    escrow = escrow_with(tmp_path)
    assert restore_escrow.read_key_escrow(escrow) == KEK
    secret = escrow / restore_escrow.KEK_SECRET_FILE
    target = tmp_path / "elsewhere.secret"
    target.write_text(KEK)
    secret.chmod(0o600)
    secret.unlink()
    secret.symlink_to(target)
    with pytest.raises(RestoreContractError, match="escrow is missing"):
        restore_escrow.read_key_escrow(escrow)


def test_a_stale_escrow_is_refused_against_the_live_source() -> None:
    restore_escrow.require_escrow_opens_source(source_client(), PROBE, KEK)
    with pytest.raises(RestoreContractError) as stale:
        restore_escrow.require_escrow_opens_source(
            source_client(digest="00" * 32), PROBE, KEK
        )
    assert str(stale.value) == restore_escrow.STALE_ESCROW
    failing = Mock(spec=ContainerPostgres)
    failing.sql.side_effect = RestoreContractError("container command failed")
    with pytest.raises(RestoreContractError) as unopened:
        restore_escrow.require_escrow_opens_source(failing, PROBE, KEK)
    assert str(unopened.value) == restore_escrow.STALE_ESCROW


def test_every_referenced_object_must_be_in_the_backup(tmp_path: Path) -> None:
    complete = backup_with(tmp_path / "one", OBJECT_KEY, RESULT_KEY)
    assert restore_escrow.require_object_backup(source_client(), complete) == 2
    partial = backup_with(tmp_path / "two", OBJECT_KEY)
    with pytest.raises(RestoreContractError) as missing:
        restore_escrow.require_object_backup(source_client(), partial)
    assert str(missing.value) == (
        "restore drill failed: 1 attachment object(s) are missing from the object "
        "backup"
    )
    with pytest.raises(RestoreContractError, match="object backup is missing"):
        restore_escrow.require_object_backup(source_client(), tmp_path / "absent")
    forged = source_client(manifest=f"../{OBJECT_KEY}|{ORGANIZATION}|x|1\n")
    with pytest.raises(RestoreContractError, match="manifest row is invalid"):
        restore_escrow.require_object_backup(forged, complete)


def test_objects_restore_into_one_fresh_private_store(tmp_path: Path) -> None:
    backup = backup_with(tmp_path, OBJECT_KEY, RESULT_KEY)
    destination = private_dir(tmp_path / "restored")
    restore_escrow.restore_object_backup(backup, destination)
    assert (destination / OBJECT_KEY).read_bytes() == b"synthetic ciphertext"
    assert (destination / RESULT_KEY).read_bytes() == b"synthetic ciphertext"
    with pytest.raises(RestoreContractError, match="object store root is invalid"):
        restore_escrow.restore_object_backup(backup, destination)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(RestoreContractError, match="object store root is invalid"):
        restore_escrow.restore_object_backup(backup, shared)


def test_the_escrowed_kek_becomes_a_private_store_that_is_removed(
    tmp_path: Path,
) -> None:
    work = private_dir(tmp_path / "work")
    restored = restore_escrow.restore_key_escrow(KEK, work)
    secret = restored / restore_escrow.KEK_SECRET_FILE
    assert secret.read_text() == KEK
    assert secret.stat().st_mode & 0o777 == 0o600
    assert restored.stat().st_mode & 0o777 == 0o700
    with pytest.raises(RestoreContractError, match="could not be restored"):
        restore_escrow.restore_key_escrow(KEK, work)
    assert restore_escrow.delete_restored_secrets(restored) is True
    assert not restored.exists()
    with pytest.raises(RestoreContractError, match="unreadable"):
        restore_escrow.restore_key_escrow("short", work)


def stub_rehearsal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the container, Docker and database observations inert."""
    monkeypatch.setattr(restore_rehearsal, "require_lease_claim", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_hba", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_equal_target_seed", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_empty_target", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_owner_rls_acl_posture", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_audit_chain", Mock())
    monkeypatch.setattr(restore_rehearsal, "require_key_probe", Mock())
    monkeypatch.setattr(restore_rehearsal, "_probe_restored_app_role", Mock())
    monkeypatch.setattr(restore_rehearsal, "_run_verify", Mock())
    scope = SourceScope(
        active_writer_count=0,
        empty_relation_counts=dict.fromkeys(REQUIRED_EMPTY, 0),
        foreign_audit_organization_count=0,
        identity_user_count=4,
        organization_count=1,
        tenant_data_key_count=2,
        totp_without_identity_count=0,
        unexpected_domain_relations=(),
        users_without_role_count=0,
    )
    monkeypatch.setattr(restore_rehearsal, "source_scope", Mock(return_value=scope))
    monkeypatch.setattr(
        restore_rehearsal, "migration_leaves", Mock(return_value="leaves")
    )
    monkeypatch.setattr(
        restore_rehearsal,
        "relation_fingerprints",
        Mock(return_value=dict.fromkeys(EQUALITY_RELATIONS, "a" * 32)),
    )
    monkeypatch.setattr(
        restore_rehearsal, "require_audit_content", Mock(return_value=12)
    )
    monkeypatch.setattr(
        restore_rehearsal,
        "require_equal_tenant_key_status",
        Mock(return_value="1|retired\n2|active"),
    )
    monkeypatch.setattr(restore_rehearsal, "verify_object_store", Mock(return_value=2))
    monkeypatch.setattr(
        restore_rehearsal,
        "require_sequence_headroom",
        Mock(return_value=(("audit_event_seq_seq", 13, 12),)),
    )


def containers() -> tuple[Mock, Mock]:
    source = source_client()
    target = Mock(spec=ContainerPostgres, container_id="b" * 64, database="target")
    target.sql.return_value = "[]"
    toc = "\n".join(
        f"{index}; 0 0 {entry.replace('clinic_app.', 'clinic_app ')} clinic_owner"
        for index, entry in enumerate(expected_toc(), 1)
    )
    source.dump.return_value = b"synthetic logical archive"
    source.list_archive.return_value = target.list_archive.return_value = toc
    source.require_postgresql_16_14.return_value = (
        target.require_postgresql_16_14.return_value
    ) = VersionEvidence("16.14", "16.14", "16.14")
    return source, target


def rehearse(
    tmp_path: Path, source: Mock, target: Mock, *, escrow: Path, backup: Path
) -> restore_rehearsal.RehearsalEvidence:
    work = private_dir(tmp_path / "work")
    store = private_dir(tmp_path / "attachments-restored")
    return restore_rehearsal.run_rehearsal(
        source,
        target,
        work,
        PROBE,
        store,
        source_claim=str(uuid4()),
        target_claim=str(uuid4()),
        key_escrow=escrow,
        object_backup=backup,
        target_app=Mock(spec=ContainerPostgres),
        verify_environment={"HOME": "/nonexistent"},
        probe_path=tmp_path / "probe.json",
    )


@pytest.mark.parametrize("gap", ["missing-key", "stale-key", "missing-object"])
def test_each_recovery_gap_refuses_before_the_archive_or_target_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gap: str
) -> None:
    stub_rehearsal(monkeypatch)
    source, target = containers()
    if gap == "stale-key":
        source = source_client(digest="00" * 32)
    escrow = (
        private_dir(tmp_path / "none")
        if gap == "missing-key"
        else (escrow_with(tmp_path))
    )
    backup = (
        backup_with(tmp_path, OBJECT_KEY)
        if gap == "missing-object"
        else backup_with(tmp_path, OBJECT_KEY, RESULT_KEY)
    )
    with pytest.raises(RestoreContractError, match=r"^restore drill failed: "):
        rehearse(tmp_path, source, target, escrow=escrow, backup=backup)
    source.dump.assert_not_called()
    target.restore.assert_not_called()
    assert not any((tmp_path / "attachments-restored").iterdir())
    assert not (tmp_path / "work" / "secrets-restored").exists()


def test_missing_key_invokes_no_postgresql_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_rehearsal(monkeypatch)
    commands: list[tuple[str, ...]] = []

    def runner(
        argv: tuple[str, ...],
        standard_input: bytes | None,
        environment: dict[str, str],
    ) -> bytes:
        commands.append(argv)
        if "pg_dump" in argv:
            return b"pg_dump (PostgreSQL) 16.14\n"
        if "pg_restore" in argv:
            return b"pg_restore (PostgreSQL) 16.14\n"
        return b"160014\n"

    source = ContainerPostgres("a" * 64, "source", "synthetic", runner=runner)
    target = ContainerPostgres("b" * 64, "target", "synthetic", runner=runner)
    with pytest.raises(
        RestoreContractError,
        match=r"^restore drill failed: the tenant KEK escrow is missing$",
    ):
        restore_rehearsal.run_rehearsal(
            source,
            target,
            private_dir(tmp_path / "work"),
            PROBE,
            private_dir(tmp_path / "objects"),
            source_claim=str(uuid4()),
            target_claim=str(uuid4()),
            key_escrow=private_dir(tmp_path / "empty-escrow"),
            object_backup=backup_with(tmp_path, OBJECT_KEY, RESULT_KEY),
        )
    assert commands == []


def test_a_complete_drill_restores_objects_and_keys_and_measures_rpo_and_rto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_rehearsal(monkeypatch)
    source, target = containers()
    escrow = escrow_with(tmp_path)
    backup = backup_with(tmp_path, OBJECT_KEY, RESULT_KEY)
    observations: list[str] = []
    read_escrow = restore_escrow.read_key_escrow

    def read(path: Path) -> str:
        observations.append("escrow")
        return read_escrow(path)

    monkeypatch.setattr(restore_rehearsal, "read_key_escrow", read)

    def source_version() -> VersionEvidence:
        observations.append("source-version")
        return VersionEvidence("16.14", "16.14", "16.14")

    def target_version() -> VersionEvidence:
        observations.append("target-version")
        return VersionEvidence("16.14", "16.14", "16.14")

    source.require_postgresql_16_14.side_effect = source_version
    target.require_postgresql_16_14.side_effect = target_version
    evidence = rehearse(tmp_path, source, target, escrow=escrow, backup=backup)
    assert observations == ["escrow", "source-version", "target-version"]
    target.restore.assert_called_once_with(b"synthetic logical archive")
    restored = tmp_path / "attachments-restored"
    assert (restored / OBJECT_KEY).is_file()
    assert (restored / RESULT_KEY).is_file()
    # The KEK used on the target is the escrowed one, and it is removed after.
    probe_call = restore_rehearsal.require_key_probe.call_args  # type: ignore[attr-defined]
    assert probe_call.kwargs["kek"] == KEK
    # The restored workflows run against the escrowed secret store, not the source's.
    verify_call = restore_rehearsal._run_verify.call_args  # type: ignore[attr-defined]
    assert verify_call.args[0]["CLINIC_SECRET_DIR"] == str(
        tmp_path / "work" / "secrets-restored"
    )
    assert evidence.key_escrow_restored is True
    assert evidence.restored_secrets_absent is True
    assert not (tmp_path / "work" / "secrets-restored").exists()
    assert (evidence.object_backup_objects, evidence.acknowledged_writes_lost) == (2, 0)
    assert evidence.rpo_measured_seconds >= 0
    assert evidence.rto_measured_seconds >= 0
    assert (evidence.provider_pitr_verified, evidence.logical_restore_only) == (
        False,
        True,
    )


def test_a_source_written_after_the_recovery_point_fails_the_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_rehearsal(monkeypatch)
    before = dict.fromkeys(EQUALITY_RELATIONS, "a" * 32)
    after = {**before, EQUALITY_RELATIONS[0]: "b" * 32}
    monkeypatch.setattr(
        restore_rehearsal,
        "relation_fingerprints",
        Mock(side_effect=[before, before, after]),
    )
    source, target = containers()
    with pytest.raises(RestoreContractError, match="changed after the recovery point"):
        rehearse(
            tmp_path,
            source,
            target,
            escrow=escrow_with(tmp_path),
            backup=backup_with(tmp_path, OBJECT_KEY, RESULT_KEY),
        )
    assert not (tmp_path / "work" / "secrets-restored").exists()


def drill_report(evidence: restore_rehearsal.RehearsalEvidence) -> JsonObject:
    """Build the published drill evidence exactly as ``main`` does."""
    return {
        **restore_rehearsal._evidence_json(evidence),
        "elapsed_seconds": 41.5,
        "receipt_statement": (
            "logical restore only; provider PITR not verified; "
            "encrypted provider backup not verified"
        ),
        "schema_version": 2,
    }


def exercise(drill: JsonObject) -> JsonObject:
    return {
        "drill": drill,
        "evidence_level": "L1",
        "exercise": "synthetic-logical-recovery",
        "failure_variants": [
            {
                "exit_code": 2,
                "message": restore_escrow.MISSING_ESCROW,
                "refused": True,
                "variant": "missing-key",
            },
            {
                "exit_code": 2,
                "message": restore_escrow.STALE_ESCROW,
                "refused": True,
                "variant": "stale-key",
            },
            {
                "exit_code": 2,
                "message": (
                    "restore drill failed: 1 attachment object(s) are missing from "
                    "the object backup"
                ),
                "refused": True,
                "variant": "missing-object",
            },
        ],
        "finished_at": "2026-11-11T09:00:00.000000Z",
        "key_rotation": {
            "dek_versions": 2,
            "rewrapped_versions": 2,
            "rotated_before_backup": True,
        },
        "receipt_statement": (
            "logical restore only; provider PITR not verified; "
            "encrypted provider backup not verified"
        ),
        "schema_version": 1,
        "source_revision": {"clean": True, "commit": "0" * 40, "tree": "1" * 40},
        "started_at": "2026-11-11T08:55:00.000000Z",
        "synthetic": True,
        "verdict": "passed",
    }


class Validator(Protocol):
    def is_valid(self, instance: object) -> bool: ...
    def iter_errors(self, instance: object) -> list[object]: ...


def validator() -> Validator:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    draft = import_module("jsonschema").Draft202012Validator
    draft.check_schema(schema)
    checker: Validator = draft(schema)
    return checker


def test_the_exercise_schema_is_draft_2020_and_recursively_closed() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    objects = _object_schemas(schema)
    assert len(objects) >= 10
    assert all(item.get("additionalProperties") is False for item in objects)


@pytest.fixture
def report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JsonObject:
    stub_rehearsal(monkeypatch)
    source, target = containers()
    evidence = rehearse(
        tmp_path,
        source,
        target,
        escrow=escrow_with(tmp_path),
        backup=backup_with(tmp_path, OBJECT_KEY, RESULT_KEY),
    )
    return exercise(drill_report(evidence))


def test_the_committed_exercise_record_is_valid_when_present() -> None:
    record = ROOT / "docs/operations/recovery-exercise.json"
    if not record.exists():
        assert latest_recovery_exercise(record).state == "absent"
        return
    assert validator().is_valid(json.loads(record.read_text(encoding="utf-8")))
    assert latest_recovery_exercise(record).state == "recorded"


def test_the_code_published_report_matches_the_schema(report: JsonObject) -> None:
    errors = sorted(validator().iter_errors(report), key=str)
    assert errors == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("drill", "provider_pitr_verified"), True),
        (("drill", "encrypted_provider_backup_verified"), True),
        (("drill", "logical_restore_only"), False),
        (("drill", "key_escrow_restored"), False),
        (("drill", "schema_version"), 1),
        (("drill", "recovery_objectives", "acknowledged_writes_lost"), 1),
        (("drill", "recovery_objectives", "rpo_proposal_seconds"), 60),
        (("failure_variants", 0, "refused"), False),
        (("failure_variants", 2, "message"), "restore drill failed: maybe"),
        (("source_revision", "clean"), False),
        (("verdict",), "failed"),
        (("evidence_level",), "L2"),
        (("synthetic",), False),
    ],
)
def test_a_report_claiming_more_than_the_drill_proved_is_rejected(
    report: JsonObject, path: tuple[str | int, ...], value: object
) -> None:
    claimed: Any = copy.deepcopy(report)
    node: Any = claimed
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    assert not validator().is_valid(claimed)


def test_a_report_missing_a_variant_or_adding_a_field_is_rejected(
    report: JsonObject,
) -> None:
    dropped: Any = copy.deepcopy(report)
    dropped["failure_variants"] = dropped["failure_variants"][:2]
    assert not validator().is_valid(dropped)
    reordered: Any = copy.deepcopy(report)
    reordered["failure_variants"] = list(reversed(reordered["failure_variants"]))
    assert not validator().is_valid(reordered)
    extra = copy.deepcopy(report)
    extra["provider_recovery"] = "verified"
    assert not validator().is_valid(extra)
    nested: Any = copy.deepcopy(report)
    nested["drill"]["pitr_window_seconds"] = 0
    assert not validator().is_valid(nested)


def acceptance_inputs(work: Path) -> restore_rehearsal_acceptance.RestoreInputs:
    return restore_rehearsal_acceptance.RestoreInputs(
        work / "probe.json",
        private_dir(work / "attachments-restored"),
        escrow_with(work),
        backup_with(work, OBJECT_KEY, RESULT_KEY),
    )


def test_the_exercise_refusal_variants_each_remove_exactly_one_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = private_dir(tmp_path / "work")
    inputs = acceptance_inputs(work)
    stale = "cd" * 32
    attempts: list[str] = []

    def attempt(
        _source: object,
        _target: object,
        _work: Path,
        variant: restore_rehearsal_acceptance.RestoreInputs,
        _evidence: Path | None = None,
    ) -> tuple[int, str]:
        if variant.key_escrow != inputs.key_escrow:
            secret = variant.key_escrow / restore_escrow.KEK_SECRET_FILE
            if secret.exists():
                assert secret.read_text() == stale
                message = restore_escrow.STALE_ESCROW
            else:
                message = restore_escrow.MISSING_ESCROW
        else:
            assert variant.object_backup != inputs.object_backup
            present = {
                path.relative_to(variant.object_backup).as_posix()
                for path in variant.object_backup.rglob("*")
                if path.is_file()
            }
            assert len(present) == 1
            message = (
                "restore drill failed: 1 attachment object(s) are missing from "
                "the object backup"
            )
        attempts.append(message)
        return (
            2,
            f"Traceback\nRestoreContractError: {message}\n",
        )

    monkeypatch.setattr(restore_rehearsal_acceptance, "_attempt_restore", attempt)
    variants: Any = restore_rehearsal_acceptance._failure_variants(
        Mock(), Mock(), work, inputs, stale
    )
    assert [item["variant"] for item in variants] == [
        "missing-key",
        "stale-key",
        "missing-object",
    ]
    assert len(attempts) == 3
    # The intact inputs were never modified by the variants.
    assert restore_escrow.read_key_escrow(inputs.key_escrow) == KEK
    assert (inputs.object_backup / OBJECT_KEY).is_file()
    assert (inputs.object_backup / RESULT_KEY).is_file()
    drill = copy.deepcopy(exercise({})["failure_variants"])
    assert variants == drill


@pytest.mark.parametrize(
    "outcome",
    [(0, ""), (2, "RestoreContractError: restore drill failed: something else\n")],
    ids=["accepted", "wrong-reason"],
)
def test_a_variant_that_is_not_refused_honestly_fails_the_exercise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: tuple[int, str]
) -> None:
    work = private_dir(tmp_path / "work")
    monkeypatch.setattr(
        restore_rehearsal_acceptance,
        "_attempt_restore",
        lambda *_arguments, **_keywords: outcome,
    )
    with pytest.raises(RuntimeError, match="was not refused honestly"):
        restore_rehearsal_acceptance._failure_variants(
            Mock(), Mock(), work, acceptance_inputs(work), "cd" * 32
        )


def test_the_escrow_is_a_private_copy_of_the_current_kek(tmp_path: Path) -> None:
    secrets = private_dir(tmp_path / "secrets")
    (secrets / restore_escrow.KEK_SECRET_FILE).write_text(KEK)
    escrow = restore_rehearsal_acceptance._escrow(tmp_path / "key-escrow", secrets)
    assert restore_escrow.read_key_escrow(escrow) == KEK
    assert (escrow / restore_escrow.KEK_SECRET_FILE).stat().st_mode & 0o777 == 0o400


def test_the_rotation_report_is_read_from_the_fixture_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = Mock(owner_password="synthetic")  # noqa: S106 - a placeholder, no secret
    monkeypatch.setattr(
        restore_rehearsal_acceptance, "database_url", Mock(return_value="synthetic")
    )
    monkeypatch.setattr(
        restore_rehearsal_acceptance,
        "_run",
        Mock(return_value='noise\n{"dek_versions": 2, "rewrapped_versions": 2}\n'),
    )
    assert restore_rehearsal_acceptance._rotate(lease, tmp_path) == {
        "dek_versions": 2,
        "rewrapped_versions": 2,
    }
    monkeypatch.setattr(
        restore_rehearsal_acceptance, "_run", Mock(return_value='{"dek": 2}\n')
    )
    with pytest.raises(RuntimeError, match="rotation report is invalid"):
        restore_rehearsal_acceptance._rotate(lease, tmp_path)
