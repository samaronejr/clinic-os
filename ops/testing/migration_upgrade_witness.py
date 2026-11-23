"""Populated legacy byte and audit-chain witnesses for the upgrade gate.

The base-revision seed loads this file with ``runpy.run_path`` (base app
code) and the candidate verifier imports it, so it depends only on modules
both revisions ship. The base seed loads the base's own Phase 1A restore
fixture (finalized SOAP, rendered/signed prescription, released document,
stored attachment, PIX, consent, teleconsult) into a second synthetic
tenant, then records the S1-S6 witnesses:

* S1-S4: per-row metadata, ciphertext digests and readable-byte digests of
  finalized SOAP versions, prescription PDFs/frozen inputs, signed bytes
  and downloadable attachments, each re-derived through the runtime role;
* S5: every audit chain's hash-stream digest (the candidate must keep the
  base prefix byte-for-byte and still pass ``verify_chain``);
* S6: a full per-table row count census (no table may lose rows).

The raw census reads as ``clinic_super`` (cross-tenant); every readable
value is re-derived as ``clinic_app`` NOBYPASSRLS inside its own tenant.
Only identifiers, digests and counts are written; no plaintext leaves the
database and the fixture key probe stays in the private directory.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Never, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import rfc8785
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from apps.audit.verification import AuditChainVerificationResult

SPECS: dict[str, tuple[str, ...]] = {
    "ehr_clinicaldocumentversion": (
        "id",
        "organization_id",
        "document_id",
        "template_id",
        "author_id",
        "version",
        "state",
        "content_sha256",
        "content_digest",
        "finalized_at",
        "content",
    ),
    "prescription_prescriptiondocument": (
        "id",
        "organization_id",
        "clinic_id",
        "input_digest",
        "pdf_digest",
        "pdf_bytes",
        "frozen_input",
    ),
    "prescription_signatureoperation": (
        "id",
        "organization_id",
        "clinic_id",
        "document_id",
        "state",
        "content_digest",
        "signed_digest",
        "operation_id",
        "signed_bytes",
    ),
    "ehr_clinicalattachment": ("id", "organization_id", "clinic_id", "sha256", "state"),
}
CIPHERTEXT_DIGESTS = {
    "content": "content_ciphertext_sha256",
    "pdf_bytes": "pdf_bytes_ciphertext_sha256",
    "frozen_input": "frozen_ciphertext_sha256",
    "signed_bytes": "signed_bytes_ciphertext_sha256",
}
# Synthetic-only switches the base fixture needs to drive every Phase 1A
# write path; none of them can enable a real provider.
FIXTURE_SWITCHES = {
    "BILLING_SYNTHETIC_PIX": "1",
    "CELERY_TASK_ALWAYS_EAGER": "1",
    "COMMS_SYNTHETIC_CHANNELS": "email",
    "PHYSICIAN_SYNTHETIC_REGISTRY": "1",
    "PRESCRIPTION_SYNTHETIC_SIGNING": "1",
    "TELECONSULT_SYNTHETIC_PROVIDER": "1",
}
MIN_VERIFIED_CHAINS = 2
DERIVED_DIGESTS = ("pdf_bytes_sha256", "frozen_input_sha256", "signed_bytes_sha256")
JsonMap = dict[str, object]
Json = None | bool | int | float | str | list["Json"] | dict[str, "Json"]


def _fail(reason: str) -> Never:
    raise RuntimeError(reason)


def _require(condition: object, reason: str) -> None:
    if not condition:
        _fail(reason)


def _normalized(value: object) -> object:
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return None if value is None else str(value)


def _digest(value: object) -> str:
    if isinstance(value, (bytes, memoryview)):
        return hashlib.sha256(bytes(value)).hexdigest()
    return hashlib.sha256(rfc8785.dumps(cast("Json", value))).hexdigest()


def _row_metadata(columns: tuple[str, ...], row: tuple[object, ...]) -> JsonMap:
    metadata: JsonMap = {}
    for key, value in zip(columns, row, strict=True):
        if key in CIPHERTEXT_DIGESTS:
            _require(
                value is None or isinstance(value, (bytes, memoryview)),
                f"witness {key} must be a stored byte envelope",
            )
            metadata[CIPHERTEXT_DIGESTS[key]] = (
                None if value is None else _digest(value)
            )
        else:
            metadata[key] = _normalized(value)
    return metadata


def super_dsn() -> str:
    """Rebind the job's ``clinic_super`` URL to the disposable upgrade DB."""
    database = urlsplit(os.environ["APP_DATABASE_URL"]).path
    original = urlsplit(os.environ["TEST_SUPERUSER_DATABASE_URL"])
    return urlunsplit(original._replace(path=database))


def _tables(connection: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    rows = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='clinic_app' "
        "ORDER BY tablename"
    ).fetchall()
    return [str(name) for (name,) in rows]


def snapshot(before: Mapping[str, object] | None = None) -> JsonMap:
    """Raw cross-tenant S1-S6 inventory; ``before`` limits S1-S4 to its rows."""
    tables: dict[str, list[JsonMap]] = {}
    counts: dict[str, int] = {}
    audit: dict[str, JsonMap] = {}
    with psycopg.connect(super_dsn()) as connection:
        row = connection.execute("SELECT current_user").fetchone()
        _require(row == ("clinic_super",), "witness census requires clinic_super")
        for table, columns in SPECS.items():
            rows = connection.execute(
                sql.SQL("SELECT {} FROM clinic_app.{} ORDER BY id").format(
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.Identifier(table),
                )
            ).fetchall()
            tables[table] = [_row_metadata(columns, item) for item in rows]
            if before is not None:
                retained = {
                    item["id"]
                    for item in cast("dict[str, list[JsonMap]]", before["tables"])[
                        table
                    ]
                }
                tables[table] = [
                    item for item in tables[table] if item["id"] in retained
                ]
        for name in _tables(connection):
            count = connection.execute(
                sql.SQL("SELECT count(*) FROM clinic_app.{}").format(
                    sql.Identifier(name)
                )
            ).fetchone()
            counts[name] = int(cast("tuple[int]", count)[0])
        previous = (
            cast("dict[str, JsonMap]", before["audit"]) if before is not None else {}
        )
        organizations = connection.execute(
            "SELECT DISTINCT organization_id FROM clinic_app.audit_event "
            "ORDER BY organization_id"
        ).fetchall()
        for (organization,) in organizations:
            serial = [
                [int(cast("int", seq)), bytes(prev).hex(), bytes(current).hex()]
                for seq, prev, current in connection.execute(
                    "SELECT seq, prev_hash, curr_hash FROM clinic_app.audit_event "
                    "WHERE organization_id=%s ORDER BY seq",
                    (organization,),
                ).fetchall()
                if isinstance(prev, (bytes, memoryview))
                and isinstance(current, (bytes, memoryview))
            ]
            cutoff = previous.get(str(organization), {}).get("last_seq")
            prefix = [
                item
                for item in serial
                if cutoff is None or cast("int", item[0]) <= cast("int", cutoff)
            ]
            audit[str(organization)] = {
                "count": len(serial),
                "last_seq": serial[-1][0] if serial else 0,
                "stream_sha256": _digest(serial),
                "before_prefix_sha256": _digest(prefix),
            }
    objects = sorted(
        (
            {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in Path(os.environ["EHR_ATTACHMENT_ROOT"]).rglob("*")
            if path.is_file()
        ),
        key=lambda item: (str(item["sha256"]), cast("int", item["bytes"])),
    )
    return {
        "tables": tables,
        "counts": counts,
        "audit": audit,
        "objects": objects,
        "role_proof": {"raw_catalog": "clinic_super"},
    }


@contextmanager
def _app_role() -> Iterator[None]:
    """Run as ``clinic_app``; ``tenant_context`` owns the transaction inside."""
    from django.db import connection  # noqa: PLC0415 - Django-ready callers only

    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


@contextmanager
def _checked_tenant(
    proof: dict[str, object], check: str, actor: str, organization: str
) -> Iterator[None]:
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from django.db import connection  # noqa: PLC0415

    with tenant_context(UUID(actor), UUID(organization)), connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, rolsuper, rolbypassrls, "
            "current_setting('app.current_tenant', true), "
            "current_setting('app.current_user_id', true) "
            "FROM pg_catalog.pg_roles WHERE rolname = current_user"
        )
        row = cursor.fetchone()
        _require(
            row == ("clinic_app", False, False, organization, actor),
            f"witness {check} requires clinic_app NOBYPASSRLS in its tenant",
        )
        proof[check] = "clinic_app"
        yield


def _check_chain(result: AuditChainVerificationResult, inventory: JsonMap) -> None:
    _require(
        result.valid is True
        and result.row_count == inventory["count"]
        and (result.last_seq or 0) == inventory["last_seq"],
        "audit verification disagrees with the unfiltered witness census",
    )


def verify_snapshot(
    probe: Mapping[str, str], expected: JsonMap, markers: Mapping[str, object]
) -> int:
    """Verify every audit chain and re-derive S1-S4 readable bytes.

    Returns the number of tenant chains verified through the runtime role.
    An unknown tenant chain fails instead of being skipped.
    """
    from apps.audit.models import SYSTEM_ORG_ID  # noqa: PLC0415
    from apps.audit.services import verify_chain  # noqa: PLC0415
    from django.db import connection  # noqa: PLC0415

    proof = cast("dict[str, object]", expected["role_proof"])
    audit = cast("dict[str, JsonMap]", expected["audit"])
    system = str(SYSTEM_ORG_ID)
    if system in audit:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            row = cursor.fetchone()
        _require(row == ("clinic_owner",), "system audit requires clinic_owner")
        _check_chain(verify_chain(), audit[system])
        proof["system_audit"] = "clinic_owner"
    actors = {
        str(markers["organization_id"]): str(markers["owner_id"]),
        probe["organization_id"]: probe["physician_id"],
    }
    chains = 0
    with _app_role():
        for organization, inventory in audit.items():
            if organization == system:
                continue
            _require(organization in actors, "unexpected tenant audit chain")
            with _checked_tenant(
                proof, "tenant_audit", actors[organization], organization
            ):
                _check_chain(verify_chain(UUID(organization)), inventory)
            chains += 1
        _verify_soap(proof, expected)
        _verify_prescriptions(proof, expected, probe["physician_id"])
        _verify_attachments(proof, expected, probe["physician_id"])
    return chains


def _verify_soap(proof: dict[str, object], expected: JsonMap) -> None:
    from apps.ehr.finalization import content_digest  # noqa: PLC0415
    from apps.ehr.models import ClinicalDocumentVersion  # noqa: PLC0415

    rows = cast("dict[str, list[JsonMap]]", expected["tables"])
    finalized = 0
    for row in rows["ehr_clinicaldocumentversion"]:
        if row["finalized_at"] is None:
            continue
        with _checked_tenant(
            proof, "soap", str(row["author_id"]), str(row["organization_id"])
        ):
            version = ClinicalDocumentVersion.objects.get(pk=str(row["id"]))
            canonical = _digest(
                {
                    "v": "ehr-document-v1",
                    "template_id": str(version.template_id),
                    "template_version": version.template.version,
                    "subjective": version.subjective,
                    "objective": version.objective,
                    "assessment": version.assessment,
                    "plan": version.plan,
                }
            )
            _require(
                canonical == content_digest(version) == version.content_digest,
                "finalized SOAP digest disagrees",
            )
            if version.content_sha256:
                _require(
                    _digest(version.content) == version.content_sha256,
                    "finalized SOAP content hash disagrees",
                )
            else:
                _require(version.content == {}, "unhashed SOAP content is not empty")
        finalized += 1
    _require(finalized, "witness found no finalized SOAP version")


def _verify_prescriptions(
    proof: dict[str, object], expected: JsonMap, physician: str
) -> None:
    from apps.prescription.models import (  # noqa: PLC0415
        PrescriptionDocument,
        SignatureOperation,
    )

    rows = cast("dict[str, list[JsonMap]]", expected["tables"])
    for row in rows["prescription_prescriptiondocument"]:
        with _checked_tenant(
            proof, "prescription_pdf", physician, str(row["organization_id"])
        ):
            document = PrescriptionDocument.objects.get(pk=str(row["id"]))
            pdf = bytes(document.pdf_bytes)
            _require(
                hashlib.sha256(pdf).hexdigest() == document.pdf_digest,
                "prescription PDF digest disagrees",
            )
            _bind(row, "pdf_bytes_sha256", hashlib.sha256(pdf).hexdigest())
            frozen = cast("dict[str, object]", document.frozen_input)
            _bind(row, "frozen_input_sha256", _digest(frozen))
            _require(
                _digest({k: v for k, v in frozen.items() if k != "input_digest"})
                == document.input_digest,
                "prescription input digest disagrees",
            )
    signed = 0
    for row in rows["prescription_signatureoperation"]:
        if row["signed_digest"] is None:
            continue
        with _checked_tenant(
            proof, "signature", physician, str(row["organization_id"])
        ):
            operation = SignatureOperation.objects.get(pk=str(row["id"]))
            content = bytes(cast("bytes", operation.signed_bytes))
            _require(
                hashlib.sha256(content).hexdigest()
                == _normalized(operation.signed_digest),
                "signed bytes digest disagrees",
            )
            _bind(row, "signed_bytes_sha256", hashlib.sha256(content).hexdigest())
        signed += 1
    _require(signed, "witness found no signed operation")


def _verify_attachments(
    proof: dict[str, object], expected: JsonMap, physician: str
) -> None:
    from apps.ehr.attachments import download_attachment  # noqa: PLC0415

    rows = cast("dict[str, list[JsonMap]]", expected["tables"])
    available = 0
    for row in rows["ehr_clinicalattachment"]:
        if row["state"] != "available":
            continue
        with _checked_tenant(
            proof, "attachment", physician, str(row["organization_id"])
        ):
            attachment = download_attachment(
                clinic_id=UUID(str(row["clinic_id"])),
                attachment_id=UUID(str(row["id"])),
            )
            _require(
                hashlib.sha256(attachment.data).hexdigest() == row["sha256"],
                "attachment download digest disagrees",
            )
        available += 1
    _require(available, "witness found no available attachment")


def _bind(row: JsonMap, key: str, value: str) -> None:
    """Record a readable-byte digest, or require it to equal the base's."""
    if key in row:
        _require(row[key] == value, f"witness {key} changed across the upgrade")
    row[key] = value


def _run_fixture(
    operation: str, environment: Mapping[str, str], *extra: str, fd: int = -1
) -> None:
    command = [sys.executable, "-m", "ops.testing.restore_fixture", operation, *extra]
    result = subprocess.run(  # noqa: S603 - fixed interpreter and module
        command,
        env=dict(environment),
        pass_fds=(fd,) if fd >= 0 else (),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        sys.stderr.write(result.stderr[-4000:])
        _fail(f"base restore fixture refused its {operation} step")


def _load_fixture(probe: Path) -> None:
    """Drive the base fixture's owner/app/owner handoff with a private frame."""
    owner = dict(os.environ)
    application = {**owner, "APP_DATABASE_URL": os.environ["UPGRADE_APP_DATABASE_URL"]}
    read_fd, write_fd = os.pipe()
    private_fd = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 3)
    os.close(read_fd)
    try:
        frame = (secrets.token_urlsafe(24) + "\n").encode()
        _require(os.write(write_fd, frame) == len(frame), "password frame truncated")
        os.close(write_fd)
        write_fd = -1
        _run_fixture(
            "bootstrap", owner, "--password-fd", str(private_fd), fd=private_fd
        )
    finally:
        os.close(private_fd)
        if write_fd >= 0:
            os.close(write_fd)
    _run_fixture("totp", application)
    _run_fixture(
        "seed",
        {
            **owner,
            **FIXTURE_SWITCHES,
            "BILLING_SYNTHETIC_PIX_SECRET": secrets.token_hex(16),
        },
        "--probe-path",
        str(probe),
    )


def _write_once(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def seed_witness(markers: Mapping[str, object]) -> dict[str, str]:
    """Load the base fixture and record its S1-S6 witnesses (base revision)."""
    probe_path = Path(os.environ["UPGRADE_PRIVATE_DIR"]) / "witness-probe.json"
    _load_fixture(probe_path)
    probe = cast("dict[str, str]", json.loads(probe_path.read_bytes()))
    tenant = _companion(probe)
    before = snapshot()
    tables = cast("dict[str, list[JsonMap]]", before["tables"])
    counts = cast("dict[str, int]", before["counts"])
    _require(all(tables.values()), "witness fixture left a sampled table empty")
    _require(
        counts["prescription_prescriptiondocumentrelease"] > 0,
        "witness fixture has no released prescription document",
    )
    _require(
        verify_snapshot(probe, before, markers) >= MIN_VERIFIED_CHAINS,
        "witness must verify both base tenant chains",
    )
    _write_once(Path(os.environ["UPGRADE_WITNESS_FILE"]), before)
    return tenant


def _companion(probe: Mapping[str, str]) -> dict[str, str]:
    """Register a second synthetic patient through the base service.

    Delegation, duplicate review and merge history need two enrollments in
    the witness tenant; the fixture's own patient stays the primary one.
    """
    from apps.intake.models import PatientClinicEnrollment  # noqa: PLC0415
    from apps.intake.patient_creation import create_patient  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from django.db import connection, transaction  # noqa: PLC0415

    organization = UUID(probe["organization_id"])
    clinic = UUID(probe["clinic_id"])
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization)],
        )
        enrollment, patient = (
            PatientClinicEnrollment.objects.filter(clinic_id=clinic)
            .values_list("id", "patient_id")
            .get()
        )
    with _app_role(), tenant_context(UUID(probe["owner_id"]), organization):
        companion = create_patient(
            clinic_id=clinic,
            full_name="Synthetic Upgrade Companion",
            birth_date=date(1984, 6, 2),
            idempotency_key=uuid4(),
        )
    return {
        **{
            key: probe[key]
            for key in ("organization_id", "clinic_id", "owner_id", "physician_id")
        },
        "patient_id": str(patient),
        "enrollment_id": str(enrollment),
        "companion_patient_id": str(companion.patient.pk),
        "companion_enrollment_id": str(companion.enrollment.pk),
    }


def _private_json(name: str) -> JsonMap:
    return cast("JsonMap", json.loads(Path(name).read_bytes()))


def _carry_digests(before: JsonMap, after: JsonMap) -> None:
    """Re-derivation must reproduce the base's readable-byte digests."""
    after_tables = cast("dict[str, list[JsonMap]]", after["tables"])
    for table, rows in cast("dict[str, list[JsonMap]]", before["tables"]).items():
        derived = {row["id"]: row for row in rows}
        for row in after_tables[table]:
            for key in DERIVED_DIGESTS:
                if key in derived[row["id"]]:
                    row[key] = derived[row["id"]][key]


def _inventory_failures(before: JsonMap, after: JsonMap) -> list[str]:
    """S1-S6: unchanged rows/envelopes/bytes, no count loss, audit prefixes."""
    failures: list[str] = []
    if before["role_proof"] != after["role_proof"]:
        failures.append("witness verification authority changed")
    if before["tables"] != after["tables"]:
        failures.append("witness S1-S4 row metadata, envelopes or bytes changed")
    old_objects = cast("list[JsonMap]", before["objects"])
    new_objects = cast("list[JsonMap]", after["objects"])
    if any(new_objects.count(item) < old_objects.count(item) for item in old_objects):
        failures.append("witness attachment object bytes changed")
    counts = cast("dict[str, int]", after["counts"])
    failures.extend(
        f"witness S6 row count dropped: {name}"
        for name, count in cast("dict[str, int]", before["counts"]).items()
        if counts.get(name, -1) < count
    )
    audit = cast("dict[str, JsonMap]", after["audit"])
    for organization, chain in cast("dict[str, JsonMap]", before["audit"]).items():
        current = audit.get(organization)
        if current is None or current["before_prefix_sha256"] != chain["stream_sha256"]:
            failures.append("witness S5 audit prefix changed")
        elif cast("int", current["count"]) < cast("int", chain["count"]):
            failures.append("witness S5 audit row count dropped")
    return failures


def compare_witness(markers: Mapping[str, object]) -> list[str]:
    """Candidate leaf: every S1-S6 witness must still hold."""
    private = Path(os.environ["UPGRADE_PRIVATE_DIR"]) / "witness-probe.json"
    probe = cast("dict[str, str]", _private_json(str(private)))
    before = _private_json(os.environ["UPGRADE_WITNESS_FILE"])
    after = snapshot(before)
    _carry_digests(before, after)
    try:
        verified = verify_snapshot(probe, after, markers)
    except RuntimeError as error:
        return [f"witness verification failed: {error}"]
    failures = _inventory_failures(before, after)
    if verified < MIN_VERIFIED_CHAINS:
        failures.append("witness verified fewer than two migrated tenant chains")
    _write_once(
        Path(os.environ["UPGRADE_WITNESS_FILE"]).with_name("witness-after.json"),
        {
            "audit_chains_verified": verified,
            "failures": failures,
            "role_proof": after["role_proof"],
            "counts": after["counts"],
            "audit": after["audit"],
        },
    )
    return failures
