"""Real clinic_app verification/delivery journeys in a live browser."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
import rfc8785
from django_otp.oath import TOTP
from playwright.sync_api import expect

from renewal.browser._protected import decrypt, encrypt
from renewal.browser.test_availability import (
    _sign_in_physician,
    availability_staff,
)
from renewal.browser.test_encounter import press, press_in_view, seed

if TYPE_CHECKING:
    from playwright.sync_api import Page

__all__ = ("availability_staff",)

ITEM = {
    "medication_description": " Medicamento fictício — não utilizar ",
    "strength_form": "Concentração e forma sintéticas",
    "dose": " Dose sintética digitada  ",
    "route": "Via sintética",
    "frequency": "Frequência sintética",
    "duration": "Duração sintética",
    "quantity": "Quantidade sintética",
    "instructions": "Orientações explícitas do médico. Sem sugestões automáticas.",
}
DAY = "2035-07-07"
SYNTHETIC_SECRET = b"synthetic-signing-key-not-a-credential"
SIGNATURE_HEADER = "x-synthetic-signature"
MARKER = b"\n%%SYNTHETIC-SIGNATURE\n"
END = b"\n%%END-SIGNATURE\n"


def _capture(page: Page, root: Path, name: str) -> None:
    destination = root / "document-verification" / f"{name}.png"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    page.screenshot(path=str(destination), full_page=True)
    destination.chmod(0o600)


def _seed_patient_access(staff: dict[str, str], patient: str) -> str:
    """Seed a verified email contact and a live invitation; return its code."""
    code = secrets.token_urlsafe(32)
    enrollment = str(uuid4())
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT id FROM clinic_app.intake_patientclinicenrollment "
            "WHERE patient_id = %s AND clinic_id = %s",
            [patient, staff["clinic_a"]],
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO clinic_app.intake_patientclinicenrollment "
                "(id, organization_id, clinic_id, patient_id, idempotency_key, "
                "create_fingerprint, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now())",
                [
                    enrollment,
                    staff["organization"],
                    staff["clinic_a"],
                    patient,
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
        else:
            enrollment = str(row[0])
        conn.execute(
            "INSERT INTO clinic_app.intake_patientcontact "
            "(id, organization_id, patient_id, channel, destination, "
            "destination_version, verified_version, verified_at, "
            "verification_method, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'email', %s, 1, 1, now(), 'synthetic', "
            "now(), now())",
            [
                str(uuid4()),
                staff["organization"],
                patient,
                encrypt(
                    conn,
                    "intake.patientcontact.destination",
                    b"paciente@verification.invalid",
                ),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientaccessgrant "
            "(id, organization_id, clinic_id, patient_id, enrollment_id, "
            "issued_by_id, issued_by_label, secret_hash, operations, "
            "expires_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
            "ARRAY['enrollment_view','questionnaires','booking','records',"
            "'consent','teleconsult']::varchar(32)[], %s, now())",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                patient,
                enrollment,
                staff["physician_a_id"],
                "physician",
                hashlib.sha256(code.encode()).digest(),
                datetime.now(UTC) + timedelta(hours=1),
            ],
        )
    return code


def _redeem(page: Page, base_url: str, clinic_id: str, code: str) -> None:
    page.goto(f"{base_url}/patient/access/{clinic_id}/")
    page.locator("#id_code").fill(code)
    with page.expect_navigation():
        page.locator("button[type=submit]").click()


def _operation_row(staff: dict[str, str], document: str) -> dict[str, str]:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT id, operation_id, signer_subject, content_digest, state "
            "FROM clinic_app.prescription_signatureoperation "
            "WHERE document_id = %s ORDER BY created_at DESC LIMIT 1",
            [document],
        ).fetchone()
        assert row is not None
        return {
            "id": str(row[0]),
            "operation_ref": str(row[1]),
            "signer": str(row[2]),
            "digest": str(row[3]),
            "state": str(row[4]),
        }


def _document_row(staff: dict[str, str], document: str) -> dict[str, object]:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT qr_handle, pdf_bytes, pdf_digest, patient_id "
            "FROM clinic_app.prescription_prescriptiondocument WHERE id = %s",
            [document],
        ).fetchone()
        assert row is not None
        pdf = decrypt(
            conn,
            "prescription.prescriptiondocument.pdf_bytes",
            bytes(cast("bytes", row[1])),
        )
        assert pdf is not None
        return {
            "handle": str(row[0]),
            "pdf": pdf,
            "digest": str(row[2]),
            "patient": str(row[3]),
        }


def _signed_callback(
    staff: dict[str, str], document: str
) -> tuple[dict[str, str], bytes]:
    """Build the provider's authenticated callback for the stored operation."""
    operation = _operation_row(staff, document)
    content = cast("bytes", _document_row(staff, document)["pdf"])
    manifest = {
        "v": "clinic-synthetic-signature-v1",
        "operation_id": operation["operation_ref"],
        "content_digest": operation["digest"],
        "signer": operation["signer"],
        "signed_at": datetime.now(UTC).isoformat(),
    }
    manifest["signature"] = hmac.new(
        SYNTHETIC_SECRET, rfc8785.dumps(manifest), hashlib.sha256
    ).hexdigest()
    signed_bytes = content + MARKER + rfc8785.dumps(manifest) + END
    payload = {
        "event_id": f"event-{secrets.token_hex(8)}",
        "operation_id": operation["operation_ref"],
        "status": "signed",
        "signed_bytes": base64.b64encode(signed_bytes).decode("ascii"),
    }
    body = json.dumps(payload).encode()
    headers = {
        SIGNATURE_HEADER: hmac.new(SYNTHETIC_SECRET, body, hashlib.sha256).hexdigest()
    }
    return headers, body


def _post_callback(
    page: Page, base_url: str, document: str, staff: dict[str, str]
) -> None:
    headers, body = _signed_callback(staff, document)
    response = page.request.post(
        f"{base_url}/prescription/signing/callback/synthetic-signature-v1/",
        data=body,
        headers={"Content-Type": "application/json", **headers},
    )
    assert response.status == 200


def _complete_step_up(page: Page, staff: dict[str, str]) -> None:
    """Answer the real step-up challenge with the seeded authenticator."""
    token = TOTP(bytes.fromhex(staff["totp_key"]), 30, 0, 6, 0).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    with page.expect_navigation():
        page.locator("button[type=submit]").click()


def _press_sign(page: Page, staff: dict[str, str]) -> None:
    """Submit the sign action, completing step-up when the flow demands it."""
    with page.expect_navigation():
        page.locator('button[value="sign_document"]').first.click()
    if "/auth/step-up/" in page.url:
        _complete_step_up(page, staff)
        with page.expect_navigation():
            page.locator('button[value="sign_document"]').first.click()
    expect(page.locator("[data-state]")).to_have_attribute("data-state", "signing")


def _worker(operation: str, root: Path) -> dict[str, object]:
    """Run the real clinic_app worker boundary for one delivery operation."""
    env = {
        **os.environ,
        "APP_DATABASE_URL": os.environ["CLINIC_RENEWAL_WORKER_DATABASE_URL"],
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "COMMS_SYNTHETIC_CHANNELS": "email",
        "PYTHONPATH": str(Path.cwd()) + os.pathsep + str(Path.cwd() / "tests"),
    }
    result = subprocess.run(  # noqa: S603 - fixed test entrypoint and owned DSN
        [
            sys.executable,
            "-c",
            "import django; django.setup(); "
            "from renewal.document_delivery_worker import main; main()",
            operation,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (root / "worker-document-delivery.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
    return cast("dict[str, object]", json.loads(result.stdout))


def _provision_physician_profile(staff: dict[str, str]) -> None:
    """Provision the synthetic signing identity through the owner boundary."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_physicianprofile "
            "(id,organization_id,user_id,jurisdiction,registration_number,"
            "signing_subject,synthetic,status) "
            "VALUES (%s,%s,%s,'SP',%s,%s,true,'unknown')",
            [
                str(uuid4()),
                staff["organization"],
                staff["physician_a_id"],
                "SYNTHETIC-CRM-35",
                f"synthetic:physician:{staff['physician_a_id']}",
            ],
        )


def _delivery_operation(staff: dict[str, str], document: str) -> str:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT id FROM clinic_app.comms_integrationoperation "
            "WHERE subject_id = %s AND subject_type = 'prescription.document' "
            "ORDER BY created_at DESC LIMIT 1",
            [document],
        ).fetchone()
        assert row is not None
        return str(row[0])


def test_document_verification_journey(  # noqa: PLR0915 - one linear journey
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    seed(staff, DAY)
    _provision_physician_profile(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    draft_url = f"{base}/prescription/clinics/{staff['clinic_a']}/draft/"
    try:
        _sign_in_physician(page, base, staff)
        page.goto(f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{DAY}/1/")
        press(page, "open")
        with page.expect_navigation():
            page.get_by_role("button", name="Prescrição sintética").click()
        press(page, "create")
        for field, value in ITEM.items():
            page.locator(f"#id_items-0-{field}").fill(value)
        press(page, "save")
        press_in_view(page, "render_document")
        _capture(page, root, "rendered")
        document = page.locator("[data-document]").first.get_attribute("data-document")
        assert document
        _press_sign(page, staff)
        _post_callback(page, base, document, staff)
        page.goto(draft_url)
        expect(page.locator("[data-document]").first).to_contain_text("Ensaio")
        _capture(page, root, "signed")

        row = _document_row(staff, document)
        handle = str(row["handle"])
        patient_id = str(row["patient"])
        verify_url = f"{base}/prescription/verify/{handle}/"

        # Anonymous verification: minimal status, no patient content.
        anon = browser.new_context().new_page()
        verified = anon.goto(verify_url)
        assert verified is not None
        assert verified.status == 200
        expect(anon.locator("#verify-status")).to_have_attribute(
            "data-status", "rehearsal_complete"
        )
        assert "Paciente" not in anon.locator("#verify-details").inner_text()
        assert str(row["digest"]) in anon.content()
        _capture(anon, root, "public-verified")

        # Release + patient download through a real redeemed session.
        press_in_view(page, "release_document")
        code = _seed_patient_access(staff, patient_id)
        patient = browser.new_context().new_page()
        _redeem(patient, base, staff["clinic_a"], code)
        patient.goto(f"{base}/patient/documents/")
        expect(patient.locator(f'[data-document="{document}"]')).to_be_visible()
        _capture(patient, root, "patient-documents")
        with patient.expect_download() as download_info:
            patient.locator('button[value="download"]').click()
        download = download_info.value
        assert download.suggested_filename.endswith(".pdf")

        # Another patient cannot reach the document.
        other_patient_id = str(uuid4())
        with psycopg.connect(staff["dsn"]) as conn:
            conn.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [staff["organization"]],
            )
            conn.execute(
                "INSERT INTO clinic_app.intake_patient "
                "(id, organization_id, full_name, birth_date, created_at) "
                "VALUES (%s, %s, %s, %s, now())",
                [
                    other_patient_id,
                    staff["organization"],
                    encrypt(
                        conn,
                        "intake.patient.full_name",
                        b"Paciente Sem Documento",
                    ),
                    encrypt(conn, "intake.patient.birth_date", b"1991-01-01"),
                ],
            )
        other_code = _seed_patient_access(staff, other_patient_id)
        other = browser.new_context().new_page()
        _redeem(other, base, staff["clinic_a"], other_code)
        forbidden = other.request.post(
            f"{base}/patient/documents/",
            form={"action": "download", "document_id": document},
        )
        assert forbidden.status == 403
        _capture(other, root, "other-patient-denied")

        # Approved delivery goes through the shared outbox, link-only.
        press_in_view(page, "deliver_document")
        operation_id = _delivery_operation(staff, document)
        receipt = _worker(operation_id, root)
        assert receipt["outcomes"] == ["succeeded"]
        assert receipt["runtime_role"] == "clinic_app"

        # Supersession: amend, render and sign a newer version.
        for field, value in ITEM.items():
            page.locator(f"#id_items-0-{field}").fill(
                value if field != "dose" else "Dose substituída"
            )
        press(page, "save")
        press_in_view(page, "render_document")
        newer = page.locator("[data-document]").last.get_attribute("data-document")
        assert newer is not None
        assert newer != document
        _press_sign(page, staff)
        _post_callback(page, base, newer, staff)
        page.goto(draft_url)
        superseded = anon.goto(verify_url)
        assert superseded is not None
        assert superseded.status == 200
        expect(anon.locator("#verify-status")).to_have_attribute(
            "data-status", "superseded"
        )
        _capture(anon, root, "public-superseded")
        verified_new = anon.goto(
            f"{base}/prescription/verify/{_document_row(staff, newer)['handle']}/"
        )
        assert verified_new is not None
        assert verified_new.status == 200
        expect(anon.locator("#verify-status")).to_have_attribute(
            "data-status", "rehearsal_complete"
        )

        # Revocation publishes immediately; the patient download closes.
        press_in_view(page, "revoke_document")
        revoked = anon.goto(verify_url)
        assert revoked is not None
        assert revoked.status == 200
        expect(anon.locator("#verify-status")).to_have_attribute(
            "data-status", "revoked"
        )
        _capture(anon, root, "public-revoked")
        denied = patient.request.post(
            f"{base}/patient/documents/",
            form={"action": "download", "document_id": document},
        )
        assert denied.status == 403

        # Unknown handles are indistinguishable and bounded per probe.
        unknown = anon.goto(f"{base}/prescription/verify/{uuid4().hex}{'a' * 11}/")
        assert unknown is not None
        assert unknown.status == 200
        expect(anon.locator("#verify-status")).to_have_attribute(
            "data-status", "unavailable"
        )
        statuses = [anon.request.get(verify_url).status for _ in range(40)]
        assert 429 in statuses
        _capture(anon, root, "rate-limited")
        assert errors == []
        (root / "document-verification" / "report.json").write_text(
            json.dumps(
                {
                    "document": document,
                    "superseded_by": newer,
                    "delivery_operation": operation_id,
                    "delivery_outcome": receipt["outcomes"],
                    "page_errors": errors,
                    "rate_limit_observed": True,
                    "statuses": [
                        "rehearsal_complete",
                        "superseded",
                        "revoked",
                        "unavailable",
                    ],
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()
