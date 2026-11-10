"""Synthetic import worlds and sources; no authority or storage is mocked."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID, uuid4

from apps.identity.models import UserClinicRole
from apps.interop import services, tasks
from apps.interop.models import ImportBatch
from apps.tenancy.db import tenant_context

from auth.stepup_test_support import create_role_actor
from identity.lifecycle_test_support import session_key, verified_client
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    import pytest

    from rbac_fixtures import RbacGraph

PATIENT_HEADER: Final = (
    "Codigo",
    "Nome",
    "Nome social",
    "Nascimento",
    "CPF",
    "Sexo",
    "Cadastrado em",
    "Cadastrado por",
)
HISTORY_HEADER: Final = (
    "Registro",
    "Paciente",
    "Tipo",
    "Data",
    "Profissional",
    "Texto",
    "Medida",
    "Valor",
    "Unidade",
    "Arquivo",
    "Titulo",
)
PATIENT_COLUMNS: Final = {
    "source_id": "Codigo",
    "legal_name": "Nome",
    "social_name": "Nome social",
    "birth_date": "Nascimento",
    "cpf": "CPF",
    "sex_at_birth": "Sexo",
    "registered_at": "Cadastrado em",
    "registered_by": "Cadastrado por",
}
HISTORY_COLUMNS: Final = {
    "source_id": "Registro",
    "patient_source_id": "Paciente",
    "kind": "Tipo",
    "recorded_at": "Data",
    "author": "Profissional",
    "text": "Texto",
    "code": "Medida",
    "value": "Valor",
    "unit": "Unidade",
    "file": "Arquivo",
    "title": "Titulo",
}
SYNTHETIC_PDF: Final = b"%PDF-1.4\n% Sintetico laudo importado\n%%EOF\n"


def part(document: Mapping[str, object], *keys: str | int) -> Any:  # noqa: ANN401
    """Read one nested report value; reports are closed JSON documents."""
    value: Any = document
    for key in keys:
        value = value[key]
    return value


@dataclass(frozen=True, slots=True)
class ImportWorld:
    """One organization with an import operator holding a fresh step-up session."""

    graph: RbacGraph
    operator: UUID
    session: str


def import_world(graph: RbacGraph, role: str = "clinic_manager") -> ImportWorld:
    """Create an operator at clinic A and verify its session over HTTP."""
    actor = create_role_actor(graph, UserClinicRole.Role(role))
    key = session_key(verified_client(replace(graph, clinic_admin=actor.pk)))
    return ImportWorld(graph=graph, operator=actor.pk, session=key)


@contextmanager
def operating(world: ImportWorld, actor: UUID | None = None) -> Iterator[None]:
    """Run as the real runtime role inside the actor's tenant transaction."""
    with (
        runtime_role(),
        tenant_context(actor or world.operator, world.graph.organization_a),
    ):
        yield


def cpf(number: int) -> str:
    """Return a check-digit-valid synthetic CPF for a positive number."""
    digits = [int(character) for character in f"{100000000 + number:09d}"[-9:]]
    for size in (9, 10):
        total = sum(
            digit * weight
            for digit, weight in zip(digits, range(size + 1, 1, -1), strict=True)
        )
        remainder = total % 11
        digits.append(0 if remainder < 2 else 11 - remainder)
    return "".join(str(digit) for digit in digits)


def patient_row(number: int, **overrides: str) -> list[str]:
    """One synthetic legacy-system patient row."""
    birth = date(1950, 1, 1) + timedelta(days=number % 20000)
    values = {
        "Codigo": f"SIN-{number:06d}",
        "Nome": f"Paciente Sintetico {number:06d}",
        "Nome social": "",
        "Nascimento": birth.strftime("%d/%m/%Y"),
        "CPF": cpf(number),
        "Sexo": "F" if number % 2 else "M",
        "Cadastrado em": "05/03/2019 09:30",
        "Cadastrado por": "Recepcao Sintetica Legada",
        **overrides,
    }
    return [values[column] for column in PATIENT_HEADER]


def csv_bytes(
    header: Sequence[str], rows: Iterable[Sequence[str]], delimiter: str = ";"
) -> bytes:
    """Encode rows the way a spreadsheet export would (UTF-8 with BOM)."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def patients_csv(count: int, *, extra: Iterable[Sequence[str]] = ()) -> bytes:
    """A semicolon-delimited export of ``count`` valid patients plus extras."""
    return csv_bytes(
        PATIENT_HEADER,
        [*(patient_row(number) for number in range(1, count + 1)), *extra],
    )


def mapping(source_kind: str = "csv", *, history: bool = False) -> dict[str, object]:
    """The operator's mapping for the synthetic legacy layout."""
    document: dict[str, object] = {
        "schema": "import-mapping-v1",
        "source_kind": source_kind,
        "date_format": "DD/MM/YYYY",
        "datetime_format": "DD/MM/YYYY HH:MM",
        "datasets": {
            "patients": {
                "columns": dict(PATIENT_COLUMNS),
                "values": {"sex_at_birth": {"F": "female", "M": "male"}},
            },
            **(
                {
                    "history": {
                        "columns": dict(HISTORY_COLUMNS),
                        "values": {
                            "kind": {
                                "Evolucao": "note",
                                "Sinal vital": "observation",
                                "Anexo": "attachment",
                            }
                        },
                    }
                }
                if history
                else {}
            ),
        },
    }
    if source_kind in ("csv", "zip"):
        options = {"delimiter": ";", "encoding": "utf-8"}
        document["csv"] = {
            "patients": options,
            **({"history": options} if history else {}),
        }
    return document


def archive(
    members: dict[str, bytes], *, manifest_override: dict[str, object] | None = None
) -> bytes:
    """Build a clinic-import-archive-v1 ZIP with an exact checksum manifest."""
    manifest = manifest_override or {
        "v": "clinic-import-archive-v1",
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            for name, data in sorted(members.items())
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr("manifest.json", json.dumps(manifest))
        for name, data in sorted(members.items()):
            target.writestr(name, data)
    return buffer.getvalue()


def history_csv(rows: Iterable[Sequence[str]]) -> bytes:
    """A clinical history export bound to patients by legacy codes."""
    return csv_bytes(HISTORY_HEADER, rows)


def history_row(  # noqa: PLR0913 - one row of the synthetic legacy layout
    source: str,
    patient: str,
    kind: str,
    *,
    text: str = "",
    measure: tuple[str, str, str] = ("", "", ""),
    file: str = "",
    author: str = "Dra. Sintetica Origem",
    when: str = "12/02/2020 14:05",
) -> list[str]:
    """One synthetic history row: note, vital sign or document reference."""
    code, value, unit = measure
    return [source, patient, kind, when, author, text, code, value, unit, file, ""]


def upload(
    world: ImportWorld, data: bytes, *, kind: str = "csv"
) -> services.ImportJobView:
    """Upload as the operator with the exact declared checksum."""
    with operating(world):
        return services.upload_import(
            clinic_id=world.graph.clinic_a,
            source_kind=kind,
            data=data,
            declared_sha256=hashlib.sha256(data).hexdigest(),
            idempotency_key=uuid4(),
        )


def to_dry_run(
    world: ImportWorld,
    data: bytes,
    *,
    kind: str = "csv",
    document: dict[str, object] | None = None,
) -> services.DryRunReport:
    """Upload, profile, save + apply a mapping and dry-run one source."""
    job = upload(world, data, kind=kind)
    with operating(world):
        profiled = services.profile_import(
            clinic_id=world.graph.clinic_a, job_id=job.job_id, expected_sequence=1
        )
        saved = services.save_mapping(
            clinic_id=world.graph.clinic_a,
            code=f"legado-{uuid4().hex[:8]}",
            document=document or mapping(kind, history=kind == "zip"),
        )
        mapped = services.apply_mapping(
            clinic_id=world.graph.clinic_a,
            job_id=job.job_id,
            mapping_id=saved.mapping_id,
            expected_sequence=profiled.job.sequence,
        )
        return services.dry_run_import(
            clinic_id=world.graph.clinic_a,
            job_id=job.job_id,
            expected_sequence=mapped.sequence,
        )


def approve(
    world: ImportWorld, report: services.DryRunReport
) -> services.ImportJobView:
    """Sign off the cutover plan, acknowledging every reported issue."""
    with operating(world):
        return services.approve_import(
            clinic_id=world.graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=report.job.sequence,
            plan=services.CutoverPlan(
                rollback_window_hours=72,
                source_frozen=True,
                backup_verified=True,
                rejects_acknowledged=True,
                duplicates_acknowledged=True,
            ),
        )


def run_batches(
    world: ImportWorld, job_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Deliver every pending batch to the real bulk task, in order, until done."""
    monkeypatch.setattr(tasks, "fair_acquire", lambda **_kwargs: True)
    monkeypatch.setattr(tasks.commit_batch_job, "apply_async", lambda **_kwargs: None)
    outcomes: list[str] = []
    with runtime_role(), tenant_context(world.operator, world.graph.organization_a):
        pending = list(
            ImportBatch.objects.filter(job_id=job_id, state="pending")
            .order_by("sequence")
            .values_list("pk", flat=True)
        )
    for batch_id in pending:
        with runtime_role():
            outcomes.append(tasks.commit_batch(batch_id=str(batch_id)))
    return outcomes


def committed(
    world: ImportWorld,
    data: bytes,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str = "csv",
    document: dict[str, object] | None = None,
) -> tuple[services.DryRunReport, services.CommitReceipt]:
    """Take one source from upload to a fully committed import."""
    report = to_dry_run(world, data, kind=kind, document=document)
    approved = approve(world, report)
    with operating(world):
        receipt = services.commit_import(
            clinic_id=world.graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    outcomes = run_batches(world, report.job.job_id, monkeypatch)
    assert outcomes == ["committed"] * len(receipt.batch_ids), outcomes
    return report, receipt
