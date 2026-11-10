"""Hostile-source handling: archives, spreadsheets, JSON, scans and the contract."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import CareTeamMembership, ProfessionalRegistration
from apps.interop import contract, services, sources
from apps.interop.models import ImportedRecord, ImportFile, ImportTransition
from apps.interop.spreadsheet import is_formula_like, neutralize_cell, write_csv
from django.utils import timezone

from identity.permission_support import owner_context
from interop.support import (
    PATIENT_COLUMNS,
    SYNTHETIC_PDF,
    archive,
    committed,
    cpf,
    history_csv,
    history_row,
    import_world,
    mapping,
    operating,
    part,
    patients_csv,
    to_dry_run,
    upload,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ROOT = Path(__file__).resolve().parents[2]
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"


def test_committed_contract_document_equals_the_runtime_schema() -> None:
    assert json.loads((ROOT / "docs/interop/import-mapping-v1.json").read_text()) == (
        contract.SCHEMA
    )
    valid = mapping("zip", history=True)
    assert contract.validate_mapping(valid) == valid
    for broken in (
        {**valid, "schema": "import-mapping-v2"},
        {**valid, "extra": True},
        {**mapping("csv"), "datasets": mapping("zip", history=True)["datasets"]},
        {
            **valid,
            "datasets": {
                "patients": {"columns": {"source_id": "Codigo", "cpf": "CPF"}}
            },
        },
        {
            **valid,
            "datasets": {
                "patients": {"columns": {"source_id": "Codigo", "legal_name": "Codigo"}}
            },
        },
        {
            **valid,
            "datasets": {
                "patients": {
                    "columns": {"source_id": "Codigo", "legal_name": "Nome"},
                    "values": {"sex_at_birth": {"F": "feminino"}},
                }
            },
        },
    ):
        with pytest.raises(services.ImportInputError) as refused:
            contract.validate_mapping(broken)
        assert refused.value.code == "invalid_mapping"


def test_spreadsheet_cells_are_neutralized_without_altering_plain_text() -> None:
    for hostile in (
        "=1+1",
        "+1",
        "-1",
        "@SUM(A1)",
        "\t=1",
        "\r=1",
        "  =HYPERLINK(1)",
        "\uff1d1",
    ):
        assert is_formula_like(hostile)
        assert neutralize_cell(hostile) == "'" + hostile
    for plain in ("Paciente Sintetico", "", "12/02/2020", "a=b", "x-1"):
        assert neutralize_cell(plain) == plain
    assert write_csv(("=h",), [("-x", "ok")]) == b"'=h\n'-x,ok\n"
    with pytest.raises(TypeError):
        neutralize_cell(1)  # type: ignore[arg-type]


def _flag_encrypted(data: bytes, name: str) -> bytes:
    """Set the encryption bit on one member's local and central headers."""
    raw = bytearray(data)
    encoded = name.encode()
    for signature, offset, name_at in ((b"PK\x03\x04", 6, 30), (b"PK\x01\x02", 8, 46)):
        start = 0
        while (start := raw.find(signature, start)) != -1:
            if raw[start + name_at : start + name_at + len(encoded)] == encoded:
                raw[start + offset] |= 0x1
            start += 4
    return bytes(raw)


def _zip(members: dict[str, bytes], *, info: zipfile.ZipInfo | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in members.items():
            target.writestr(name, data)
        if info is not None:
            target.writestr(info, b"x")
    return buffer.getvalue()


def test_archives_outside_the_flat_layout_are_refused() -> None:
    good = {"patients.csv": patients_csv(1)}
    symlink = zipfile.ZipInfo("files/link.pdf")
    symlink.external_attr = (0o120777) << 16
    cases = {
        "slip": _zip({**good, "manifest.json": b"{}", "../evil.csv": b"x"}),
        "nested": _zip({**good, "manifest.json": b"{}", "files/a/b.pdf": b"x"}),
        "absolute": _zip({**good, "manifest.json": b"{}", "/etc/passwd": b"x"}),
        "symlink": _zip({**good, "manifest.json": b"{}"}, info=symlink),
        "encrypted": _flag_encrypted(
            _zip({**good, "manifest.json": b"{}", "files/locked.pdf": b"x"}),
            "files/locked.pdf",
        ),
        "bomb": _zip({**good, "manifest.json": b"{}", "files/z.txt": b"0" * 9_000_000}),
        "no_manifest": _zip(good),
        "no_patients": _zip({"manifest.json": b"{}"}),
    }
    for name, data in cases.items():
        with pytest.raises(services.ImportInputError) as refused:
            sources.open_archive(data)
        assert refused.value.code == "archive_layout", name
    appended = io.BytesIO(archive(good))
    with zipfile.ZipFile(appended, "a") as target:
        target.writestr("files/unlisted.pdf", SYNTHETIC_PDF)
    for data in (
        appended.getvalue(),
        archive(good, manifest_override={"v": "other", "files": []}),
        _zip({**good, "manifest.json": b"not json"}),
    ):
        with pytest.raises(services.ImportInputError) as mismatch:
            sources.open_archive(data)
        assert mismatch.value.code == "manifest_mismatch"
    with pytest.raises(services.ImportInputError) as broken:
        sources.open_archive(b"PK\x03\x04 not a zip")
    assert broken.value.code == "malformed_archive"


def test_attachments_are_checksummed_and_scanned_in_quarantine(
    rbac_graph: RbacGraph,
) -> None:
    world = import_world(rbac_graph)
    members = {
        "patients.csv": patients_csv(1),
        "history.csv": history_csv(
            [
                history_row("A-1", "SIN-000001", "Anexo", file="limpo.pdf"),
                history_row("A-2", "SIN-000001", "Anexo", file="ativo.pdf"),
                history_row("A-3", "SIN-000001", "Anexo", file="falso.png"),
                history_row("A-4", "SIN-000001", "Anexo", file="programa.exe"),
            ]
        ),
        "files/limpo.pdf": SYNTHETIC_PDF,
        "files/ativo.pdf": b"%PDF-1.4 /OpenAction /JavaScript (app.alert(1))",
        "files/falso.png": SYNTHETIC_PDF,
        "files/programa.exe": b"MZ synthetic",
    }
    report = to_dry_run(world, archive(members), kind="zip")
    with owner_context(rbac_graph.organization_a):
        verdicts = {
            str(row.path): (row.scan_verdict, row.scan_reason, row.sha256)
            for row in ImportFile.objects.filter(
                job_id=report.job.job_id, role="attachment"
            )
        }
    assert {path: verdict[:2] for path, verdict in verdicts.items()} == {
        "files/limpo.pdf": ("clean", ""),
        "files/ativo.pdf": ("rejected", "active_pdf_content"),
        "files/falso.png": ("rejected", "type_mismatch"),
        "files/programa.exe": ("rejected", "unsupported_type"),
    }
    assert verdicts["files/limpo.pdf"][2] == sources.sha256_hex(SYNTHETIC_PDF)
    reasons = {(item[1], item[3]) for item in part(report.report, "rejections")}
    assert reasons == {(2, "file_rejected"), (3, "file_rejected"), (4, "file_rejected")}
    assert part(report.report, "datasets", "history", "accepted") == 1


def _xlsx(
    rows: list[list[str]], *, formula_row: int | None = None, dtd: bool = False
) -> bytes:
    cells = []
    for index, row in enumerate(rows, start=1):
        values = []
        for column, value in enumerate(row):
            reference = f"{chr(65 + column)}{index}"
            if formula_row == index and column == 1:
                values.append(f'<c r="{reference}"><f>HYPERLINK("x")</f><v>1</v></c>')
            else:
                values.append(
                    f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'
                )
        cells.append(f'<row r="{index}">{"".join(values)}</row>')
    doctype = '<!DOCTYPE x [<!ENTITY e "boom">]>' if dtd else ""
    sheet = (
        f'<?xml version="1.0"?>{doctype}<worksheet xmlns="{MAIN}">'
        f"<sheetData>{''.join(cells)}</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{MAIN}" xmlns:r="{RELS}"><sheets>'
            '<sheet name="Pacientes" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        target.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{PACKAGE}"><Relationship Id="rId1" '
            'Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        target.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def test_xlsx_and_api_sources_import_their_cached_values(rbac_graph: RbacGraph) -> None:
    world = import_world(rbac_graph)
    header = ["Codigo", "Nome", "Nascimento", "CPF"]
    book = _xlsx(
        [
            header,
            ["X-1", "Paciente Planilha", "32874", cpf(31)],
            ["X-2", "Paciente Formula", "32874", cpf(32)],
            ["X-3", "Paciente Data Texto", "01/01/1990", cpf(33)],
        ],
        formula_row=3,
    )
    xlsx_mapping: dict[str, object] = {
        "schema": "import-mapping-v1",
        "source_kind": "xlsx",
        "date_format": "excel_serial",
        "datasets": {
            "patients": {
                "columns": {
                    "source_id": "Codigo",
                    "legal_name": "Nome",
                    "birth_date": "Nascimento",
                    "cpf": "CPF",
                }
            }
        },
    }
    report = to_dry_run(world, book, kind="xlsx", document=xlsx_mapping)
    assert part(report.report, "datasets", "patients") == {
        "rows": 3,
        "accepted": 1,
        "rejected": 2,
    }
    assert {(item[1], item[3]) for item in part(report.report, "rejections")} == {
        (2, "formula_cell"),
        (3, "invalid_date"),
    }
    with pytest.raises(services.ImportInputError) as unsafe:
        sources.read_xlsx(_xlsx([header], dtd=True))
    assert unsafe.value.code == "unsafe_spreadsheet"

    payload = json.dumps(
        {
            "datasets": {
                "patients": [
                    {"Codigo": "API-1", "Nome": "Paciente Api", "CPF": cpf(41)},
                    {"Codigo": "API-2", "Nome": None, "CPF": cpf(42)},
                ]
            }
        }
    ).encode()
    api_mapping: dict[str, object] = {
        "schema": "import-mapping-v1",
        "source_kind": "api",
        "datasets": {
            "patients": {
                "columns": {"source_id": "Codigo", "legal_name": "Nome", "cpf": "CPF"}
            }
        },
    }
    api = to_dry_run(world, payload, kind="api", document=api_mapping)
    assert part(api.report, "datasets", "patients") == {
        "rows": 2,
        "accepted": 1,
        "rejected": 1,
    }
    for broken in (
        b'{"datasets": {"patients": [{"a": "1", "a": "2"}]}}',
        b'{"datasets": {"patients": [{"a": 1}]}}',
        b'{"datasets": {"other": []}}',
        b'{"datasets": {"patients": [{"a": NaN}]}}',
    ):
        with pytest.raises(services.ImportInputError) as refused:
            sources.read_api(broken)
        assert refused.value.code in {"malformed_json", "empty_dataset"}


def test_profile_and_advisory_suggestion_carry_no_patient_values(
    rbac_graph: RbacGraph,
) -> None:
    world = import_world(rbac_graph)
    job = upload(world, patients_csv(3))
    with operating(world):
        profiled = services.profile_import(
            clinic_id=rbac_graph.clinic_a, job_id=job.job_id, expected_sequence=1
        )
        suggestion = services.suggest_mapping(
            clinic_id=rbac_graph.clinic_a, job_id=job.job_id
        )
        saved = services.save_mapping(
            clinic_id=rbac_graph.clinic_a,
            code="sugerido",
            document=suggestion.document,
            suggested=True,
        )
        second = services.save_mapping(
            clinic_id=rbac_graph.clinic_a,
            code="sugerido",
            document=suggestion.document,
        )
        jobs = services.import_jobs(clinic_id=rbac_graph.clinic_a)
    assert contract.validate_profile(profiled.profile)
    flattened = json.dumps(profiled.profile, ensure_ascii=False)
    assert "Paciente Sintetico" not in flattened
    assert cpf(1) not in flattened
    patients = part(profiled.profile, "datasets", "patients")
    assert patients["row_count"] == 3
    assert patients["csv"] == {"delimiter": ";", "encoding": "utf-8"}
    assert suggestion.advisory is True
    assert suggestion.method == "deterministic_header_vocabulary"
    columns = part(suggestion.document, "datasets", "patients", "columns")
    assert {field: columns[field] for field in ("source_id", "legal_name", "cpf")} == {
        field: PATIENT_COLUMNS[field] for field in ("source_id", "legal_name", "cpf")
    }
    assert suggestion.confidence["patients.cpf"] == "high"
    assert (saved.version, saved.suggested, second.version) == (1, True, 2)
    assert [item.job_id for item in jobs] == [job.job_id]


def test_imported_documents_are_readable_only_through_clinical_scope(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    members = {
        "patients.csv": patients_csv(1),
        "history.csv": history_csv(
            [history_row("D-1", "SIN-000001", "Anexo", file="laudo.pdf")]
        ),
        "files/laudo.pdf": SYNTHETIC_PDF,
    }
    report, _receipt = committed(world, archive(members), monkeypatch, kind="zip")
    physician = import_world(rbac_graph, "physician").operator
    with owner_context(rbac_graph.organization_a):
        document = ImportedRecord.objects.get(
            job_id=report.job.job_id, kind="attachment"
        )
        ProfessionalRegistration.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=physician,
            role="physician",
            council="CRM",
            number="SINTETICO-066B",
            jurisdiction="SP",
            specialty="Sintetico",
            status="regular",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
        CareTeamMembership.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_enrollment_id=document.enrollment_id,
            user_id=physician,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
    with operating(world, physician):
        attachment = services.imported_attachment(
            clinic_id=rbac_graph.clinic_a, record_id=document.pk
        )
    assert attachment.data == SYNTHETIC_PDF
    assert attachment.media_type == "application/pdf"
    with operating(world), pytest.raises(services.ImportAccessDeniedError):
        services.imported_attachment(
            clinic_id=rbac_graph.clinic_a, record_id=document.pk
        )
    with owner_context(rbac_graph.organization_a):
        assert ImportTransition.objects.filter(
            job_id=report.job.job_id, state="committed"
        ).exists()
