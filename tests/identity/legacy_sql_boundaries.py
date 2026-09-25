"""Resolver-backed authorization, through the exposed PostgreSQL functions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django.db import connection
from psycopg import sql

from identity.legacy_parity_support import LEGACY, MANAGERS, PHYSICIAN, Boundary

if TYPE_CHECKING:
    from identity.legacy_operational_boundaries import OperationalSubjects
    from identity.legacy_parity_support import LegacyWorld


def query(
    name: str, args: list[str | UUID | list[str]], *, boolean: bool = False
) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT * FROM clinic_app.{}({})").format(
                sql.Identifier(name), sql.SQL(",").join(sql.Placeholder() for _ in args)
            ),
            args,
        )
        rows = cursor.fetchall()
    return rows == [(True,)] if boolean else bool(rows) and rows[0] != (None,)


def _bound_user(w: LegacyWorld, valid: bool, name: str) -> bool:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)", [str(uuid4())]
            )
    return query(name, [])


def boundaries(op: OperationalSubjects) -> tuple[Boundary, ...]:
    return (
        Boundary(
            "clinic_app.user_has_org",
            "sql",
            LEGACY,
            lambda w, ok: query(
                "user_has_org",
                [w.graph.organization_a if ok else w.graph.organization_b],
                boolean=True,
            ),
        ),
        Boundary(
            "clinic_app.user_organizations",
            "sql",
            LEGACY,
            lambda w, ok: _bound_user(w, ok, "user_organizations"),
        ),
        Boundary(
            "clinic_app.load_current_user",
            "sql",
            LEGACY,
            lambda w, ok: _bound_user(w, ok, "load_current_user"),
        ),
        Boundary(
            "clinic_app.auth_lookup",
            "sql",
            LEGACY,
            lambda w, ok: query(
                "auth_lookup", [w.actor.username if ok else "synthetic-unknown-parity"]
            ),
        ),
        Boundary(
            "clinic_app.list_active_clinic_physicians",
            "sql",
            MANAGERS,
            lambda w, ok: query("list_active_clinic_physicians", [w.clinic_for(ok)]),
        ),
        Boundary(
            "clinic_app.questionnaire_staff",
            "sql",
            LEGACY,
            lambda w, ok: query(
                "questionnaire_staff", [w.clinic_for(ok), list(LEGACY)], boolean=True
            ),
        ),
        Boundary(
            "clinic_app.questionnaire_completion",
            "sql",
            LEGACY,
            lambda w, ok: query(
                "questionnaire_completion", [w.clinic_for(ok), op.enrollment]
            ),
        ),
        Boundary(
            "clinic_app.ehr_version_scope",
            "sql",
            LEGACY,
            lambda w, ok: query("ehr_version_scope", [w.clinic_for(ok), w.version.pk]),
        ),
        Boundary(
            "clinic_app.billing_staff_invoice",
            "sql",
            MANAGERS,
            lambda w, ok: query(
                "billing_staff_invoice",
                [op.invoice.pk if ok else uuid4()],
                boolean=True,
            ),
        ),
        Boundary(
            "clinic_app.waitlist_staff",
            "sql",
            MANAGERS,
            lambda w, ok: query("waitlist_staff", [w.clinic_for(ok)], boolean=True),
        ),
        Boundary(
            "clinic_app.retention_care",
            "sql",
            PHYSICIAN,
            lambda w, ok: query(
                "retention_care",
                [w.clinic_for(ok), w.appointment.patient_id],
                boolean=True,
            ),
        ),
        Boundary(
            "clinic_app.retention_care_patients",
            "sql",
            PHYSICIAN,
            lambda w, ok: query("retention_care_patients", [w.clinic_for(ok)]),
        ),
    )
