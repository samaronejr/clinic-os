"""Bounded configuration through real runtime roles, PostgreSQL and HTTP."""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import AppointmentReminder
from apps.consent.models import ConsentText
from apps.consent.services import patient_receipts, publish_text
from apps.ehr.attachments import AttachmentInput
from apps.ehr.models import ClinicalDocumentVersion, SpecialtyTemplate
from apps.ehr.services import SOAP_FIELDS, publish_template
from apps.identity.clinic_configuration import (
    MAX_LOGO_BYTES,
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
    validated_logo,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import Clinic, ClinicConfiguration, UserClinicRole
from apps.intake.models import QuestionnaireTemplate
from apps.intake.patient_access import patient_session_context
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, connections, transaction
from PIL import Image

from auth.stepup_test_support import create_role_actor, verified_request
from otp_test_support import runtime_role as http_runtime_role
from patient_http_support import verified_physician_client
from patient_service_support import runtime_role
from renewal.test_consent import PURPOSE, TEXT, accept
from renewal.test_consent import seed as consent_seed
from renewal.test_encounters import seed as clinical_seed
from renewal.test_encounters import setup_context
from renewal.test_reminders import _contact
from renewal.test_retention import admin, finalized, staff_client
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from typing import Any
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def configuration(clinic_id: UUID, **changes: object) -> ClinicConfiguration:
    values: dict[str, Any] = {
        "expected_version": 0,
        "display_name": "Clínica Sintética",
        "contact_email": "contato@example.invalid",
        "contact_phone": "+5511999990000",
        "brand_token": "teal",
        "reminder_hours": 12,
    }
    values.update(changes)
    return publish_configuration(
        clinic_id=clinic_id,
        expected_version=values.pop("expected_version"),
        logo=values.pop("logo", None),
        remove_logo=values.pop("remove_logo", False),
        content=ConfigurationContent(**values),
    )


def png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("role", ["owner", "clinic_admin"])
def test_canonical_admin_publication_is_scoped_append_only_and_audited(
    rbac_graph: RbacGraph,
    role: str,
) -> None:
    g = rbac_graph
    actor = create_role_actor(g, UserClinicRole.Role(role))
    with runtime_role(), tenant_context(actor.pk, g.organization_a):
        first = configuration(g.clinic_a)
        second = configuration(
            g.clinic_a, expected_version=1, display_name="Outra marca"
        )
        assert (first.version, second.version) == (1, 2)
        first.refresh_from_db()
        assert first.display_name == "Clínica Sintética"
        current = latest_configuration(g.clinic_a)
        assert current is not None
        assert current.pk == second.pk
        assert latest_configuration(g.clinic_b) is None
        assert latest_configuration(g.clinic_c) is None
        assert Clinic.objects.get(pk=g.clinic_a).name != second.display_name
        for clinic in (g.clinic_b, g.clinic_c, uuid4()):
            with pytest.raises(CurrentActorError):
                configuration(clinic)
        with pytest.raises(ValidationError):
            configuration(g.clinic_a, expected_version=1)
    with setup_context(g.organization_a):
        events = list(
            AuditEvent.objects.filter(
                event_type="identity.clinic_configuration.published"
            )
        )
        assert len(events) == 2
        assert {event.affected_record_id for event in events} == {
            str(first.pk),
            str(second.pk),
        }
        assert all(
            set(event.payload) == {"clinic_id", "object_verb"} for event in events
        )
        assert all(event.actor_user_id == actor.pk for event in events)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("display_name", "<script>alert(1)</script>"),
        ("display_name", "body {display:none}"),
        ("display_name", "javascript:alert(1)"),
        ("display_name", "\x00hidden"),
        ("display_name", " "),
        ("brand_token", "#ffffff"),
        ("brand_token", "hide-warnings"),
        ("reminder_hours", 0),
        ("reminder_hours", 25),
        ("reminder_hours", True),
        ("contact_email", "not-an-email"),
        ("contact_phone", "123"),
    ],
)
def test_invalid_content_tokens_and_format_write_nothing(
    rbac_graph: RbacGraph,
    field: str,
    value: object,
) -> None:
    g = rbac_graph
    actor = admin(g)
    with runtime_role(), tenant_context(actor.pk, g.organization_a):
        with pytest.raises(ValidationError):
            configuration(g.clinic_a, **{field: value})
        assert not ClinicConfiguration.objects.exists()


def test_physician_receptionist_revoked_and_foreign_roles_cannot_edit(
    rbac_graph: RbacGraph,
) -> None:
    g = rbac_graph
    actor = admin(g)
    with setup_context(g.organization_a):
        UserClinicRole.objects.filter(user=actor, clinic_id=g.clinic_a).delete()
    for actor_id in (g.physician, g.shared_user, actor.pk):
        with (
            pytest.raises((CurrentActorError, TenantAccessDeniedError)),
            runtime_role(),
            tenant_context(actor_id, g.organization_a),
        ):
            configuration(g.clinic_a)


def test_raw_database_acl_rls_binding_and_immutability(rbac_graph: RbacGraph) -> None:
    g = rbac_graph
    actor = admin(g)
    with runtime_role(), tenant_context(actor.pk, g.organization_a):
        first = configuration(g.clinic_a)
        for statement in (
            "UPDATE clinic_app.identity_clinicconfiguration "
            "SET display_name='bad' WHERE id=%s",
            "DELETE FROM clinic_app.identity_clinicconfiguration WHERE id=%s",
        ):
            with (
                pytest.raises(DatabaseError),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement, [first.pk])
        for changes in (
            {"organization_id": g.organization_b},
            {"clinic_id": g.clinic_b},
            {"published_by_id": g.physician},
            {"reminder_hours": 999},
            {"brand_token": "#fff"},
            {"version": 10},
            {"display_name": "body {display:none}"},
            {"logo_png": b"<svg/>"},
        ):
            values = {
                "organization_id": g.organization_a,
                "clinic_id": g.clinic_a,
                "version": 2,
                "display_name": "Safe",
                "published_by_id": actor.pk,
            }
            values.update(changes)
            with pytest.raises(DatabaseError), transaction.atomic():
                ClinicConfiguration.objects.create(**values)
    with runtime_role(), tenant_context(g.physician, g.organization_a):
        assert ClinicConfiguration.objects.filter(pk=first.pk).exists()
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicConfiguration.objects.create(
                organization_id=g.organization_a,
                clinic_id=g.clinic_a,
                version=2,
                display_name="Safe",
                published_by_id=g.physician,
            )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='clinic_app.identity_clinicconfiguration'::regclass"
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute(
            "SELECT policyname FROM pg_policies WHERE schemaname='clinic_app' "
            "AND tablename='identity_clinicconfiguration'"
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "setup_tenant",
            "configuration_read",
            "configuration_insert",
        }


def test_two_editors_cannot_overwrite_each_other(rbac_graph: RbacGraph) -> None:
    g = rbac_graph
    actor = admin(g)
    barrier = Barrier(2, timeout=15)

    def publish() -> str:
        try:
            with runtime_role(), tenant_context(actor.pk, g.organization_a):
                barrier.wait()
                configuration(g.clinic_a)
                return "published"
        except ValidationError:
            return "stale"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish) for _ in range(2)]
        assert sorted(f.result(timeout=30) for f in futures) == ["published", "stale"]


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"<svg><script/></svg>", "image/svg+xml"),
        (b"PK\x03\x04zip", "image/png"),
        (b"%PDF-1.4", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n<script>", "image/png"),
        (b"\x89PNG\r\n\x1a\ntruncated", "image/png"),
        (b"x" * (MAX_LOGO_BYTES + 1), "image/png"),
    ],
)
def test_logo_attachment_policy_rejects_unsafe_and_malformed_files(
    data: bytes, mime: str
) -> None:
    with pytest.raises(ValidationError):
        validated_logo(AttachmentInput("logo", mime, data))


def test_logo_scan_fail_closed_and_normalized_retained_versions(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    g = rbac_graph
    actor = admin(g)
    upload = AttachmentInput("../../logo.png", "image/png", png())
    with runtime_role(), tenant_context(actor.pk, g.organization_a):
        first = configuration(g.clinic_a, logo=upload)
        second = configuration(g.clinic_a, expected_version=1)
        assert bytes(first.logo_png) == bytes(second.logo_png)
        removed = configuration(g.clinic_a, expected_version=2, remove_logo=True)
        assert not removed.logo_png
        first.refresh_from_db()
        assert bytes(first.logo_png).startswith(b"\x89PNG")
        settings.CLINIC_DATA_MODE = "live"
        with pytest.raises(ValidationError):
            configuration(g.clinic_a, expected_version=3, logo=upload)
        assert ClinicConfiguration.objects.count() == 3


def test_consent_and_finalized_document_retain_exact_old_versions(
    rbac_graph: RbacGraph,
) -> None:
    g = rbac_graph
    text, session, _, manager = consent_seed(g)
    with runtime_role(), patient_session_context(session):
        receipt = accept(text)
    appointment, template = clinical_seed(g)
    request = verified_request(g.physician)
    with runtime_role(), tenant_context(g.physician, g.organization_a):
        version = finalized(g, appointment, template, request)
        digest = version.content_digest
    with runtime_role(), tenant_context(manager, g.organization_a):
        configuration(g.clinic_a)
        updated_text = publish_text(
            clinic_id=g.clinic_a, purpose=PURPOSE, text="Nova versão sintética"
        )
        updated_template = publish_template(
            clinic_id=g.clinic_a,
            key=template.key,
            title="Modelo novo",
            prompts=dict.fromkeys(SOAP_FIELDS, "Orientação nova"),
        )
        assert updated_text.version == 2
        assert updated_template.version == 2
        with pytest.raises(ValidationError):
            publish_text(
                clinic_id=g.clinic_a, purpose=PURPOSE, text="<script>bad</script>"
            )
        with pytest.raises(ValidationError):
            publish_template(
                clinic_id=g.clinic_a,
                key=template.key,
                title="Safe",
                prompts=dict.fromkeys(SOAP_FIELDS, "color: white"),
            )
        for model, pk, field in (
            (ConsentText, text.pk, "text"),
            (SpecialtyTemplate, template.pk, "title"),
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                model.objects.filter(pk=pk).update(**{field: "retroactive"})
    with runtime_role(), patient_session_context(session):
        stored = patient_receipts()[0]
        assert stored.pk == receipt.pk
        assert stored.text_id == text.pk
        assert stored.text.text == TEXT
        assert stored.text.digest == text.digest
    with runtime_role(), tenant_context(g.physician, g.organization_a):
        stored_version = ClinicalDocumentVersion.objects.get(pk=version.pk)
        assert stored_version.state == "finalized"
        assert stored_version.template_id == template.pk
        assert stored_version.content_digest == digest


def test_reminder_schedule_changes_only_new_booking_snapshots(
    rbac_graph: RbacGraph,
) -> None:
    g = rbac_graph
    actor = admin(g)
    setup = seed_appointment_setup(g)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _contact(setup, "email")
        appointment = create_synthetic_appointment(setup)
        old = AppointmentReminder.objects.select_related("operation").get(
            appointment=appointment
        )
        old_due = old.operation.not_before
        assert old_due == appointment.start_at - timedelta(hours=24)
    with runtime_role(), tenant_context(actor.pk, g.organization_a):
        configuration(g.clinic_a, reminder_hours=6)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        new = create_synthetic_appointment(
            setup, start_local="2035-06-02T10:00", end_local="2035-06-02T10:30"
        )
        reminder = AppointmentReminder.objects.select_related("operation").get(
            appointment=new
        )
        assert reminder.operation.not_before == new.start_at - timedelta(hours=6)
        old.operation.refresh_from_db()
        assert old.operation.not_before == old_due


def test_settings_http_prg_csrf_unknown_fields_timezone_and_logo(
    rbac_graph: RbacGraph,
) -> None:
    g = rbac_graph
    actor = admin(g)
    url = f"/clinics/{g.clinic_a}/settings/"
    values = {
        "action": "settings",
        "expected_version": "0",
        "display_name": "Marca sintética",
        "contact_email": "",
        "contact_phone": "",
        "brand_token": "teal",
        "reminder_hours": "12",
    }
    with staff_client(actor.pk) as client:
        assert client.get(url).status_code == 200
        for field in (
            "timezone",
            "owner_database_url",
            "css",
            "roles",
            "warning_visibility",
            "version",
        ):
            assert client.post(url, {**values, field: "forbidden"}).status_code == 400
        for field in ("contact_email", "display_name"):
            assert (
                client.post(
                    url,
                    {
                        **values,
                        field: SimpleUploadedFile("unexpected.png", png(), "image/png"),
                    },
                ).status_code
                == 400
            )
        assert (
            client.post(
                url,
                {**values, "logo": SimpleUploadedFile("logo.png", png(), "image/png")},
            ).status_code
            == 302
        )
        response = client.get(url)
        assert response.status_code == 200
        assert {"no-store", "private"} <= set(response["Cache-Control"].split(", "))
        logo = client.get(url + "logo/")
        assert logo.status_code == 200
        assert logo["Content-Type"] == "image/png"
        assert logo["X-Content-Type-Options"] == "nosniff"
        assert client.post(url, values).status_code == 400
        assert (
            client.post(f"/clinics/{g.clinic_b}/settings/", values).status_code == 403
        )
        client.handler.enforce_csrf_checks = True
        assert client.post(url, {**values, "expected_version": "1"}).status_code == 403
    with runtime_role(), tenant_context(actor.pk, g.organization_a):
        assert ClinicConfiguration.objects.count() == 1
        assert str(Clinic.objects.get(pk=g.clinic_a).timezone) == "America/Sao_Paulo"
    client = verified_physician_client(g)
    with http_runtime_role():
        assert client.get(url).status_code == 403
        assert client.post(url, values).status_code == 403


def test_questionnaire_publication_uses_fixed_typed_rows(
    rbac_graph: RbacGraph,
) -> None:
    """Admins publish typed versions; malformed rows write nothing."""
    g = rbac_graph
    actor = admin(g)
    url = f"/clinics/{g.clinic_a}/settings/"
    values = {
        "action": "questionnaire",
        "key": "pre",
        "title": "Pré-consulta sintética",
        "q1_label": "Motivo da consulta",
        "q1_type": "text",
        "q1_required": "on",
        "q1_max_length": "",
        "q1_options": "",
        "q2_label": "Retorno por",
        "q2_type": "selection",
        "q2_max_length": "",
        "q2_options": "Telefone\r\nMensagem\r\n",
        "q3_label": "Alergia conhecida?",
        "q3_type": "boolean",
        "q3_max_length": "40",
        "q3_options": "",
    }
    with staff_client(actor.pk) as client:
        page = client.get(url)
        assert b'id="questionnaire_key"' in page.content
        assert b'id="id_key"' in page.content  # specialty keeps its own id
        rejected = client.post(url, {**values, "q1_options": "Sim"})
        assert rejected.status_code == 400
        assert client.post(url, {**values, "q9_label": "x"}).status_code == 400
        assert client.post(url, values).status_code == 302
    with runtime_role(), tenant_context(actor.pk, g.organization_a):
        (template,) = QuestionnaireTemplate.objects.filter(clinic_id=g.clinic_a)
    assert template.version == 1
    assert template.questions == [
        {
            "id": "q_1",
            "label": "Motivo da consulta",
            "type": "text",
            "required": True,
            "max_length": 500,
            "options": [],
        },
        {
            "id": "q_2",
            "label": "Retorno por",
            "type": "selection",
            "required": False,
            "max_length": 200,
            "options": ["Telefone", "Mensagem"],
        },
        {
            "id": "q_3",
            "label": "Alergia conhecida?",
            "type": "boolean",
            "required": False,
            "max_length": 5,
            "options": [],
        },
    ]
    client = verified_physician_client(g)
    with http_runtime_role():
        assert client.post(url, values).status_code == 403
