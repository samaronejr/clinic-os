"""Drive the finalizer as the actual requester, with real archive bytes staged."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.interop.export_builder import _claim, write_archive
from apps.interop.models import OrganizationExport, OrganizationExportPart
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from django.utils import timezone

from ai.pending_admin_cases import care_case
from interop.support import org_admin
from interop.test_org_export import _archive, _receipt, _request, dispatched
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from rbac_fixtures import RbacGraph

__all__ = ("dispatched",)
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("dispatched"),
]


@dataclass(frozen=True)
class Prepared:
    key: UUID
    origins: frozenset[tuple[str, str]]


def _digest(keys: frozenset[tuple[str, str]]) -> str:
    return hashlib.sha256(
        "".join(f"{table}/{key}\n" for table, key in sorted(keys)).encode("ascii")
    ).hexdigest()


def _prepare(g: RbacGraph, admin: UUID, path: Path) -> Prepared:
    request = _request(g, admin)
    with runtime_role():
        scope = _claim(request.export_id, g.organization_a, admin)
        assert scope is not None
        facts, manifest = write_archive(scope, path)
    data = path.read_bytes()
    part_hash = hashlib.sha256(data).hexdigest()
    with runtime_role(), tenant_context(admin, g.organization_a):
        OrganizationExportPart.objects.create(
            organization_id=g.organization_a,
            export_id=request.export_id,
            part=1,
            data=data,
            sha256=part_hash,
            size_bytes=len(data),
            created_at=timezone.now(),
        )
        assert (
            OrganizationExport.objects.filter(pk=request.export_id).update(
                part_count=1,
                archive_bytes=len(data),
                archive_sha256=part_hash,
                manifest_sha256=manifest,
                dataset_count=len(facts.datasets),
                record_count=facts.records,
                attachment_count=len(facts.attachments),
            )
            == 1
        )
    return Prepared(request.export_id, frozenset(facts.pending_origins))


def _finalize(key: UUID, clinic: UUID, digest: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT clinic_app.interop_finalize_export(%s,%s,%s)", [key, clinic, digest]
        )


def _refusal(key: UUID, clinic: UUID, digest: str) -> tuple[str, str | None]:
    with pytest.raises(DatabaseError) as captured, transaction.atomic():
        _finalize(key, clinic, digest)
    cause = captured.value.__cause__
    assert isinstance(cause, psycopg.errors.InsufficientPrivilege)
    assert cause.sqlstate is not None
    return cause.sqlstate, cause.diag.message_primary


def test_finalizer_has_identical_wrong_clinic_unknown_and_repeat_denials(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    g = rbac_graph
    admin = org_admin(g)
    job = _prepare(g, admin, tmp_path / "source.zip")
    digest = _digest(job.origins)
    with runtime_role(), tenant_context(admin, g.organization_a):
        wrong = _refusal(job.key, g.clinic_b, digest)
        unknown = _refusal(uuid4(), g.clinic_a, digest)
        # The original column grant is unchanged, but a runtime caller cannot
        # substitute its own ready transition for the checked resolver.
        with pytest.raises(DatabaseError), transaction.atomic():
            OrganizationExport.objects.filter(pk=job.key).update(
                state="ready", revision=F("revision") + 1
            )
        assert OrganizationExport.objects.get(pk=job.key).state == "building"
        _finalize(job.key, g.clinic_a, digest)
        repeat = _refusal(job.key, g.clinic_a, digest)
        assert wrong == unknown == repeat == ("42501", "interop export denied")
        assert OrganizationExport.objects.get(pk=job.key).revision == 3
    assert (
        _archive(g, admin, _receipt(g, admin, job.key))
        == (tmp_path / "source.zip").read_bytes()
    )


@pytest.mark.parametrize("refuse_during", [False, True], ids=["excluded", "in-flight"])
def test_finalizer_refuses_excluded_or_subsequently_withdrawn_origin(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    synthetic_secret_backend: Path,
    refuse_during: bool,
) -> None:
    g = rbac_graph
    case = care_case(g, monkeypatch, synthetic_secret_backend)
    assert all(case.displays(hx) for hx in (False, True))
    if not refuse_during:
        case.change_choice()
    admin = org_admin(g)
    job = _prepare(g, admin, tmp_path / "source.zip")
    care_keys = frozenset((case.relation, key) for key in case.keys)
    if refuse_during:
        assert care_keys <= job.origins
        case.change_choice()
    else:
        assert care_keys.isdisjoint(job.origins)
    with runtime_role(), tenant_context(admin, g.organization_a):
        assert _refusal(job.key, g.clinic_a, _digest(job.origins | care_keys)) == (
            "42501",
            "interop export denied",
        )
        current = OrganizationExport.objects.get(pk=job.key)
        assert current.state == "building"
        assert current.pending_origins is None
        if not refuse_during:
            _finalize(job.key, g.clinic_a, _digest(job.origins))
            current.refresh_from_db()
            assert current.state == "ready"
            assert current.build_origins is None
            assert current.pending_origins is not None
            assert {tuple(pair) for pair in current.pending_origins} == job.origins
