"""Organization export v2 (v1 frozen): complete, organization-scoped, verifiable."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.ehr.attachments import scan_attachment
from apps.identity.models import User, UserClinicRole
from apps.intake.services import create_patient
from apps.interop import export_builder, tasks
from apps.interop.errors import InteropAccessDeniedError
from apps.interop.export_builder import build_organization_export, neutralize
from apps.interop.export_registry import EXCLUSION_REASONS, FORMAT, datasets
from apps.interop.management import (
    create_client,
    download_export_part,
    list_exports,
    request_export,
)
from apps.interop.migrations import _export_v2_sql
from apps.interop.migrations._export_sql import EXCLUDED_V1, EXPORT_V1
from apps.interop.migrations._export_v2_sql import (
    EXCLUDED_V2,
    EXPORT_V2,
    IMPORT_EXCLUSIONS,
    IMPORT_EXPORTS,
)
from apps.interop.migrations._interop_sql import TABLES
from apps.interop.rls import IMPORT_RLS_TABLES
from apps.retention.services import verify_export_package
from apps.subscriptions.services import cancel_subscription
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.test import override_settings
from ops.testing import restore_contract

from auth.stepup_test_support import create_role_actor
from identity.permission_support import owner_context
from interop.support import (
    SYNTHETIC_PDF,
    committed,
    history_csv,
    history_row,
    import_world,
    org_admin,
    org_b_admin,
    patients_csv,
)
from interop.support import archive as import_archive
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from renewal.test_attachments import PDF_BYTES, attachment_root, begin, upload
from renewal.test_retention import staff_client
from subscriptions.support import event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from typing import Any

    from apps.ehr.models import ClinicalAttachment
    from apps.interop.export_registry import Dataset
    from apps.interop.management import ExportView
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

__all__ = ("attachment_root",)
pytestmark = pytest.mark.django_db(transaction=True)

MANAGEMENT = "/api/interop/v1/"
FORMAT_DOCUMENT = Path(__file__).resolve().parents[2] / "docs" / "api" / "export-v2.md"
V1_DOCUMENT = FORMAT_DOCUMENT.with_name("export-v1.md")
FORMULA = "=SOMA(1;2)"
FOREIGN_NAME = "Paciente Sintetica Organizacao B"
AUDIT_KEYS = {
    "clinic_id",
    "http_method",
    "http_status",
    "object_verb",
    "reason_code",
    "request_id",
}
STAFF_COLUMNS = {
    "id",
    "username",
    "first_name",
    "last_name",
    "email",
    "is_active",
    "date_joined",
    "last_login",
}
EXPORTED = [entry[0] for entry in EXPORT_V2]
IMPORT_RELATIONS = {*IMPORT_RLS_TABLES, "interop_hiddenenrollment"}


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Capture the bulk-queue hand-offs instead of publishing them."""
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        tasks.build_export, "apply_async", lambda **options: sent.append(options)
    )
    return sent


def _request(graph: RbacGraph, actor: UUID, command: UUID | None = None) -> ExportView:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        return request_export(clinic_id=graph.clinic_a, command_id=command or uuid4())


def _build(graph: RbacGraph, actor: UUID, job: ExportView) -> str:
    with runtime_role():
        return build_organization_export(
            export_id=job.export_id,
            organization_id=graph.organization_a,
            requested_by_id=actor,
        )


def _receipt(graph: RbacGraph, actor: UUID, export_id: UUID) -> ExportView:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        (job,) = [
            row
            for row in list_exports(clinic_id=graph.clinic_a)
            if row.export_id == export_id
        ]
    return job


def _archive(graph: RbacGraph, actor: UUID, job: ExportView) -> bytes:
    """Download every part and prove each digest the receipt and parts declare."""
    with runtime_role(), tenant_context(actor, graph.organization_a):
        parts = [
            download_export_part(
                clinic_id=graph.clinic_a, export_id=job.export_id, part=number
            )
            for number in range(1, job.part_count + 1)
        ]
    assert all(hashlib.sha256(part.data).hexdigest() == part.sha256 for part in parts)
    archive = b"".join(part.data for part in parts)
    assert hashlib.sha256(archive).hexdigest() == job.archive_sha256
    assert len(archive) == job.archive_bytes
    return archive


def _files(archive: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        return {name: package.read(name) for name in package.namelist()}


def _jsonl(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.splitlines()]


def _available_attachment(graph: RbacGraph) -> ClinicalAttachment:
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        attachment = upload(graph, encounter)
        return scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)


def _foreign_patient(graph: RbacGraph) -> str:
    """Register one organization B patient; return its receptionist's username."""
    user = User.objects.create(
        username=f"sintetico-org-b-recepcao-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
    )
    with owner_context(graph.organization_b):
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user=user,
            role="receptionist",
        )
    with runtime_role(), tenant_context(user.pk, graph.organization_b):
        create_patient(
            clinic_id=graph.clinic_c,
            full_name=FOREIGN_NAME,
            birth_date=date(1990, 3, 4),
            idempotency_key=uuid4(),
        )
    return user.username


def _stored(database_url: str, export_id: UUID) -> tuple[str, str, int, int]:
    """Read past RLS: state, failure reason, part rows and parts still holding bytes."""
    with psycopg.connect(database_url) as raw:
        row = raw.execute(
            "SELECT e.state, e.failure_reason, count(p.part), count(p.data) "
            "FROM clinic_app.interop_organizationexport e "
            "LEFT JOIN clinic_app.interop_organizationexportpart p "
            "ON p.export_id=e.id WHERE e.id=%s GROUP BY e.id",
            [export_id],
        ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), int(row[2]), int(row[3])


def _expire_now(database_url: str, export_id: UUID) -> None:
    """Move one archive's expiry into the past with the guard lifted for one txn."""
    with psycopg.connect(database_url) as raw, raw.transaction():
        raw.execute("SET LOCAL session_replication_role = replica")
        raw.execute(
            "UPDATE clinic_app.interop_organizationexport "
            "SET expires_at=statement_timestamp()-interval '1 second' WHERE id=%s",
            [export_id],
        )


def _revoke(database_url: str, user: UUID, clinic: UUID) -> None:
    with psycopg.connect(database_url) as raw:
        raw.execute(
            "UPDATE clinic_app.identity_userclinicrole SET revoked_at=now() "
            "WHERE user_id=%s AND clinic_id=%s",
            [user, clinic],
        )


def _then(
    first: Dataset, action: Callable[[], None], rest: list[Dataset]
) -> Iterator[Dataset]:
    """Yield the first dataset, run ``action``, then yield the remaining ones."""
    yield first
    action()
    yield from rest


def _manage(
    http: Client, action: str, clinic_id: UUID, **terms: object
) -> _MonkeyPatchedWSGIResponse:
    body = {"action": action, "clinic_id": str(clinic_id), **terms}
    return http.post(MANAGEMENT, json.dumps(body), content_type="application/json")


def test_export_registry_partitions_every_recovery_relation() -> None:
    assert len(EXPORTED) == len(set(EXPORTED))
    assert set(EXPORTED).isdisjoint(EXCLUDED_V2)
    assert set(EXPORTED) | set(EXCLUDED_V2) == set(restore_contract.DOMAIN_RELATIONS)
    assert set(TABLES) <= set(restore_contract.DOMAIN_RELATIONS)
    assert set(EXCLUDED_V2.values()) <= set(EXCLUSION_REASONS)
    by_relation = {dataset.relation: dataset for dataset in datasets()}
    assert list(by_relation) == EXPORTED
    for relation, key, _kind, omitted, scope in EXPORT_V2:
        names = {column.name for column in by_relation[relation].columns}
        assert key in names, relation
        assert names.isdisjoint(omitted), relation
        assert scope in {"organization", "self", "staff"}, relation
    staff = {column.name for column in by_relation["identity_user"].columns}
    assert staff == STAFF_COLUMNS


def test_export_v1_stays_frozen_and_v2_adds_exactly_the_import_relations() -> None:
    # v1 still partitions exactly the manifest it was frozen against.
    v1 = {entry[0] for entry in EXPORT_V1} | set(EXCLUDED_V1)
    assert v1 == set(restore_contract.DOMAIN_RELATIONS) - IMPORT_RELATIONS
    assert {entry[0] for entry in IMPORT_EXPORTS} | set(IMPORT_EXCLUSIONS) == (
        IMPORT_RELATIONS
    )
    assert tuple(sorted((*EXPORT_V1, *IMPORT_EXPORTS))) == EXPORT_V2
    assert EXCLUDED_V2 == {**EXCLUDED_V1, **IMPORT_EXCLUSIONS}
    # The v2 migration replaces only the reader's branches; its reverse is v1's.
    assert _export_v2_sql.FORWARD.count("requested_relation='") == len(EXPORT_V2)
    assert _export_v2_sql.REVERSE.count("requested_relation='") == len(EXPORT_V1)
    assert "GRANT SELECT ON" not in _export_v2_sql.FORWARD + _export_v2_sql.REVERSE


def test_live_reader_is_one_static_branch_per_exported_relation() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.prosrc, p.prosecdef, pg_get_userbyid(p.proowner), "
            "has_function_privilege('clinic_app', p.oid, 'EXECUTE') "
            "FROM pg_proc p WHERE p.oid="
            "'clinic_app.interop_export_rows(uuid,text,text,integer)'::regprocedure"
        )
        row = cursor.fetchone()
    assert row is not None
    source, definer, owner, runtime = row
    assert (definer, owner, runtime) == (True, "clinic_resolver", True)
    assert re.findall(r"requested_relation='([a-z0-9_]+)'", source) == EXPORTED
    assert "EXECUTE" not in source.upper()
    # A stored import source is never read: only attachments carry their bytes.
    assert source.count(_export_v2_sql.SEALED_SOURCE) == 1


@pytest.mark.parametrize(
    ("path", "format_id", "excluded", "exported"),
    [
        (V1_DOCUMENT, "clinic-organization-export-v1", EXCLUDED_V1, EXPORT_V1),
        (FORMAT_DOCUMENT, FORMAT, EXCLUDED_V2, EXPORT_V2),
    ],
    ids=["v1", "v2"],
)
def test_format_document_names_every_exclusion_and_omitted_column(
    path: Path,
    format_id: str,
    excluded: Mapping[str, str],
    exported: tuple[tuple[str, str, str, tuple[str, ...], str], ...],
) -> None:
    document = path.read_text(encoding="utf-8")
    assert f"Format id: `{format_id}`" in document
    rows = {
        line.split("|")[1].strip(" `"): line
        for line in document.splitlines()
        if line.startswith("| `")
    }
    for relation, reason in excluded.items():
        assert f"`{relation}`" in rows[reason], relation
    for relation, _key, _kind, omitted, _scope in exported:
        for column in omitted:
            assert f"`{column}`" in rows[relation], (relation, column)


def test_csv_cells_never_start_with_a_formula_trigger() -> None:
    for trigger in ("=1+1", "+1", "-1", "@SOMA(A1)", "\tx", "\rx"):
        assert neutralize(trigger) == "'" + trigger
        assert neutralize("Paciente " + trigger) == "Paciente " + trigger


def _assert_manifest(
    manifest: dict[str, Any], graph: RbacGraph, job: ExportView
) -> None:
    assert manifest["v"] == FORMAT
    assert manifest["export_id"] == str(job.export_id)
    assert manifest["organization_id"] == str(graph.organization_a)
    assert manifest["manifest_sha256"] == job.manifest_sha256
    assert [entry["relation"] for entry in manifest["datasets"]] == EXPORTED
    excluded = {entry["relation"]: entry["reason"] for entry in manifest["excluded"]}
    assert excluded == EXCLUDED_V2
    counts = (
        manifest["dataset_count"],
        manifest["record_count"],
        manifest["attachment_count"],
    )
    assert counts == (len(EXPORTED), job.record_count, 1)
    assert sum(entry["records"] for entry in manifest["datasets"]) == job.record_count
    paths = {entry["path"] for entry in manifest["files"]}
    for entry in manifest["datasets"]:
        assert {entry["jsonl"], entry["csv"]} <= paths


def _assert_content(files: dict[str, bytes], admin: UUID) -> None:
    patients = _jsonl(files["data/intake_patient.jsonl"])
    assert [(row["full_name"], row["birth_date"]) for row in patients] == [
        ("Synthetic Booking Persona", "2000-01-02")
    ]
    table = io.StringIO(files["data/interop_apiclient.csv"].decode("utf-8"))
    assert [row["name"] for row in csv.DictReader(table)] == ["'" + FORMULA]
    clients = _jsonl(files["data/interop_apiclient.jsonl"])
    assert [row["name"] for row in clients] == [FORMULA]
    staff = _jsonl(files["data/identity_user.jsonl"])
    assert str(admin) in {row["id"] for row in staff}
    assert all(set(row) == STAFF_COLUMNS for row in staff)
    assert not [name for name in files if "secret" in name or "token" in name]


def _assert_audit(graph: RbacGraph, job: ExportView) -> None:
    with owner_context(graph.organization_a):
        events = list(
            AuditEvent.objects.filter(
                event_type__startswith="interop.organization_export."
            ).values_list("event_type", "payload")
        )
    assert sorted(kind for kind, _payload in events) == sorted(
        [
            "interop.organization_export.requested",
            "interop.organization_export.ready",
            *["interop.organization_export.downloaded"] * job.part_count,
        ]
    )
    assert all(set(payload) <= AUDIT_KEYS for _kind, payload in events)


def test_org_admin_export_is_complete_scoped_and_verifiable(
    rbac_graph: RbacGraph,
    attachment_root: Path,
    dispatched: list[dict[str, object]],
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    attachment = _available_attachment(graph)
    with runtime_role(), tenant_context(admin, graph.organization_a):
        create_client(
            clinic_id=graph.clinic_a,
            name=FORMULA,
            scopes=["appointments:read"],
            rate_limit_per_minute=60,
            command_id=uuid4(),
        )
    foreign_username = _foreign_patient(graph)
    command = uuid4()
    job = _request(graph, admin, command)
    assert (job.state, job.part_count, job.archive_sha256) == ("requested", 0, "")
    # Only opaque references travel to the bulk worker.
    assert dispatched == [
        {
            "kwargs": {
                "export_id": str(job.export_id),
                "organization_id": str(graph.organization_a),
                "requested_by_id": str(admin),
            },
            "queue": "bulk",
        }
    ]
    # A replayed command returns the same receipt and hands off nothing new.
    assert _request(graph, admin, command).export_id == job.export_id
    assert len(dispatched) == 1
    with override_settings(INTEROP_EXPORT_PART_BYTES=16 * 1024):
        assert _build(graph, admin, job) == "ready"
        # A redelivered message finds nothing left to claim.
        assert _build(graph, admin, job) == "skipped"
    ready = _receipt(graph, admin, job.export_id)
    assert ready.state == "ready"
    assert ready.part_count > 1
    assert ready.expires_at is not None
    assert ready.finished_at is not None
    assert ready.expires_at - ready.finished_at == timedelta(days=7)
    archive = _archive(graph, admin, ready)
    verdict = verify_export_package(archive)
    assert (verdict.ok, verdict.manifest_digest) == (True, ready.manifest_sha256)
    files = _files(archive)
    _assert_manifest(json.loads(files["manifest.json"]), graph, ready)
    assert files[f"attachments/{attachment.pk}.pdf"] == PDF_BYTES
    _assert_content(files, admin)
    everything = b"\n".join(files.values())
    for foreign in (
        str(graph.organization_b),
        str(graph.clinic_c),
        FOREIGN_NAME,
        foreign_username,
        "Todo 8 Organization B",
        "Todo 8 Clinic C",
    ):
        assert foreign.encode() not in everything, foreign
    _assert_audit(graph, ready)


def test_http_management_downloads_and_refuses_foreign_exports_alike(
    rbac_graph: RbacGraph, dispatched: list[dict[str, object]]
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    foreign_admin = org_b_admin(graph)
    with staff_client(admin) as http:
        requested = _manage(
            http, "exports.request", graph.clinic_a, command_id=str(uuid4())
        )
        assert requested.status_code == 202
        assert "no-store" in requested.headers["Cache-Control"]
        export_id = UUID(requested.json()["export_id"])
        # The signed-in client already runs as the runtime role the worker uses.
        assert (
            build_organization_export(
                export_id=export_id,
                organization_id=graph.organization_a,
                requested_by_id=admin,
            )
            == "ready"
        )
        listed = _manage(http, "exports.list", graph.clinic_a).json()["exports"]
        assert [(row["export_id"], row["state"]) for row in listed] == [
            (str(export_id), "ready")
        ]
        download = _manage(
            http, "exports.download", graph.clinic_a, export_id=str(export_id), part=1
        )
        beyond = _manage(
            http, "exports.download", graph.clinic_a, export_id=str(export_id), part=2
        )
        unknown = _manage(
            http, "exports.download", graph.clinic_a, export_id=str(uuid4()), part=1
        )
    assert download.status_code == 200
    assert download.headers["Content-Disposition"] == (
        f'attachment; filename="organizacao-{export_id}.zip"'
    )
    assert download.headers["X-Export-Part"] == "1/1"
    assert "no-store" in download.headers["Cache-Control"]
    digest = hashlib.sha256(download.content).hexdigest()
    assert digest == download.headers["X-Export-Archive-Sha256"]
    assert verify_export_package(download.content).ok
    with staff_client(foreign_admin) as http:
        foreign = _manage(
            http, "exports.download", graph.clinic_c, export_id=str(export_id), part=1
        )
        crossed = _manage(
            http, "exports.download", graph.clinic_a, export_id=str(export_id), part=1
        )
    for refused in (beyond, unknown, foreign, crossed):
        assert (refused.status_code, refused.json()) == (403, {"code": "not_available"})


def test_export_needs_org_wide_authority_and_stops_once_it_is_lost(
    rbac_graph: RbacGraph,
    dispatched: list[dict[str, object]],
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    clinic_only = create_role_actor(graph, UserClinicRole.Role.ORG_ADMIN).pk
    for actor in (clinic_only, graph.physician):
        with (
            runtime_role(),
            tenant_context(actor, graph.organization_a),
            pytest.raises(InteropAccessDeniedError),
        ):
            request_export(clinic_id=graph.clinic_a, command_id=uuid4())
    admin = org_admin(graph)
    job = _request(graph, admin)
    first, *rest = datasets()
    revoke = partial(_revoke, superuser_database_url, admin, graph.clinic_b)
    monkeypatch.setattr(export_builder, "datasets", lambda: _then(first, revoke, rest))
    assert _build(graph, admin, job) == "failed"
    # The requester can no longer write; the sweep records the failure.
    assert _stored(superuser_database_url, job.export_id) == ("building", "", 0, 0)
    with runtime_role():
        assert tasks.expire() == 1
    stored = _stored(superuser_database_url, job.export_id)
    assert stored == ("failed", "authority_revoked", 0, 0)


def test_missing_attachment_bytes_fail_the_export_without_parts(
    rbac_graph: RbacGraph,
    attachment_root: Path,
    dispatched: list[dict[str, object]],
    superuser_database_url: str,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    attachment = _available_attachment(graph)
    (attachment_root / attachment.storage_key).unlink()
    job = _request(graph, admin)
    assert _build(graph, admin, job) == "failed"
    failed = _receipt(graph, admin, job.export_id)
    assert (failed.state, failed.failure_reason, failed.part_count) == (
        "failed",
        "attachment_unavailable",
        0,
    )
    assert _stored(superuser_database_url, job.export_id)[2:] == (0, 0)
    with (
        runtime_role(),
        tenant_context(admin, graph.organization_a),
        pytest.raises(InteropAccessDeniedError),
    ):
        download_export_part(clinic_id=graph.clinic_a, export_id=job.export_id, part=1)


def test_offboarded_organization_still_exports_its_records(
    rbac_graph: RbacGraph, dispatched: list[dict[str, object]]
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    with runtime_role(), tenant_context(admin, graph.organization_a):
        cancel_subscription(
            clinic_id=graph.clinic_a, expected_revision=1, command_id=uuid4()
        )
    assert event(graph, admin, "offboard").state == "offboarded"
    job = _request(graph, admin)
    assert _build(graph, admin, job) == "ready"
    archive = _archive(graph, admin, _receipt(graph, admin, job.export_id))
    assert verify_export_package(archive).ok
    subscriptions = _jsonl(_files(archive)["data/subscriptions_subscription.jsonl"])
    assert [row["state"] for row in subscriptions] == ["offboarded"]


def test_lost_handoffs_are_redispatched_by_reference_only(
    rbac_graph: RbacGraph, dispatched: list[dict[str, object]]
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    pending = _request(graph, admin)
    built = _request(graph, admin)
    assert _build(graph, admin, built) == "ready"
    dispatched.clear()
    with runtime_role():
        due = tasks.dispatch_due()
    assert {
        "kwargs": {
            "export_id": str(pending.export_id),
            "organization_id": str(graph.organization_a),
            "requested_by_id": str(admin),
        },
        "queue": "bulk",
    } in dispatched
    assert due == len(dispatched)
    assert str(built.export_id) not in repr(dispatched)


def test_expired_archives_keep_receipts_and_lose_their_bytes(
    rbac_graph: RbacGraph,
    dispatched: list[dict[str, object]],
    superuser_database_url: str,
) -> None:
    graph = rbac_graph
    admin = org_admin(graph)
    job = _request(graph, admin)
    assert _build(graph, admin, job) == "ready"
    ready = _receipt(graph, admin, job.export_id)
    with runtime_role():
        assert tasks.expire() == 0
    _expire_now(superuser_database_url, job.export_id)
    with runtime_role():
        assert tasks.expire() == 1
        assert tasks.expire() == 0
    expired = _receipt(graph, admin, job.export_id)
    assert (expired.state, expired.archive_sha256, expired.part_count) == (
        "expired",
        ready.archive_sha256,
        ready.part_count,
    )
    stored = _stored(superuser_database_url, job.export_id)
    assert stored == ("expired", "", ready.part_count, 0)
    with (
        runtime_role(),
        tenant_context(admin, graph.organization_a),
        pytest.raises(InteropAccessDeniedError),
    ):
        download_export_part(clinic_id=graph.clinic_a, export_id=job.export_id, part=1)


def test_v2_exports_committed_imports_but_never_a_stored_source(
    rbac_graph: RbacGraph,
    dispatched: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    world = import_world(graph)
    members = {
        "patients.csv": patients_csv(1),
        "history.csv": history_csv(
            [history_row("D-1", "SIN-000001", "Anexo", file="laudo.pdf")]
        ),
        "files/laudo.pdf": SYNTHETIC_PDF,
    }
    report, _receipt_view = committed(
        world, import_archive(members), monkeypatch, kind="zip"
    )
    admin = org_admin(graph)
    job = _request(graph, admin)
    assert _build(graph, admin, job) == "ready"
    package = _archive(graph, admin, _receipt(graph, admin, job.export_id))
    assert verify_export_package(package).ok
    files = _files(package)
    manifest = json.loads(files["manifest.json"])
    assert manifest["v"] == "clinic-organization-export-v2"
    excluded = {entry["relation"]: entry["reason"] for entry in manifest["excluded"]}
    assert excluded["interop_importbatch"] == "machinery"
    assert "data/interop_importbatch.jsonl" not in files
    jobs = _jsonl(files["data/interop_importjob.jsonl"])
    assert [row["id"] for row in jobs] == [str(report.job.job_id)]
    transitions = _jsonl(files["data/interop_importtransition.jsonl"])
    assert "committed" in {row["state"] for row in transitions}
    assert len(_jsonl(files["data/interop_importmapping.jsonl"])) == 1
    records = {
        row["kind"]: row for row in _jsonl(files["data/interop_importedrecord.jsonl"])
    }
    assert set(records) == {"patient", "attachment"}
    assert records["attachment"]["source_record_id"] == "D-1"
    stored = {
        row["role"]: row for row in _jsonl(files["data/interop_importfile.jsonl"])
    }
    assert set(stored) == {"source", "attachment"}
    assert base64.b64decode(str(stored["attachment"]["data"])) == SYNTHETIC_PDF
    assert "data" not in stored["source"]
