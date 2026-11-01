"""Committed-state observations independent of the HTTP connection's RLS role."""

from __future__ import annotations

import psycopg


def refusal_state(database_url: str) -> tuple[object, ...]:
    # signed_in holds clinic_app on Django's connection. Inspect committed rows
    # independently so RLS cannot turn a real effect into an invisible zero.
    with psycopg.connect(database_url) as inspection:
        totals = inspection.execute(
            "SELECT (SELECT count(*) FROM clinic_app.ai_aiartifact), "
            "(SELECT count(*) FROM clinic_app.ai_aiinvocation), "
            "(SELECT count(*) FROM clinic_app.comms_integrationoperation), "
            "(SELECT count(*) FROM clinic_app.audit_event), "
            "(SELECT count(*) FROM clinic_app.ai_extractionjob), "
            "(SELECT count(*) FROM clinic_app.orders_resultobservation), "
            "(SELECT count(*) FROM clinic_app.orders_resultacknowledgment)"
        ).fetchone()
        assert totals is not None
        return (
            *totals,
            tuple(
                inspection.execute(
                    "SELECT id,status,observation_id,reviewed_by_id,reviewed_at "
                    "FROM clinic_app.ai_extractioncandidate ORDER BY id"
                ).fetchall()
            ),
            tuple(
                inspection.execute(
                    "SELECT id,revision,state "
                    "FROM clinic_app.ehr_clinicaldocumentversion ORDER BY id"
                ).fetchall()
            ),
            tuple(
                inspection.execute(
                    "SELECT session_key,session_data,expire_date "
                    "FROM clinic_app.django_session ORDER BY session_key"
                ).fetchall()
            ),
        )
