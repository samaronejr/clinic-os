from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.audit.services import (
    AuditEventInput,
    AuditEventValueRejectedError,
    AuditPayloadValueRejectedError,
    _normalize_payload,
    record_event,
    verify_chain,
)
from django.db import connection, transaction
from psycopg.errors import InvalidParameterValue

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from psycopg.rows import TupleRow


@dataclass(frozen=True, slots=True)
class DirectAuditInput:
    event_type: str = "audit.synthetic"
    component_id: str = "boundary-test"
    affected_record_type: str | None = None
    affected_record_id: str | None = None
    payload: str = "{}"


def _event() -> AuditEventInput:
    return AuditEventInput(
        event_type="audit.synthetic",
        component_id="boundary-test",
        component_ip=None,
        affected_record_type=None,
        affected_record_id=None,
        occurred_at_utc=datetime.now(UTC),
    )


def _set_context(
    raw_connection: psycopg.Connection[TupleRow],
    organization_id: UUID,
    actor_user_id: UUID,
) -> None:
    raw_connection.execute(
        "SELECT set_config('app.current_tenant', %s, true), "
        "set_config('app.current_user_id', %s, true)",
        (str(organization_id), str(actor_user_id)),
    )


def _tenant_append(
    raw_connection: psycopg.Connection[TupleRow],
    event: DirectAuditInput,
) -> int:
    row = raw_connection.execute(
        "SELECT clinic_app.audit_append(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            event.event_type,
            event.component_id,
            None,
            event.affected_record_type,
            event.affected_record_id,
            datetime.now(UTC),
            event.payload,
            b"1" * 32,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _owner_database_url() -> str:
    return database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )


@pytest.mark.parametrize(
    ("event_type", "component_id", "affected_type", "affected_id"),
    [
        ("x" + (" " * 128), "component", None, None),
        ("event", "x" + (" " * 255), None, None),
        ("event", "component", "x" + (" " * 128), "record"),
        ("event", "component", "record", "x" + (" " * 255)),
        ("\t\n", "component", None, None),
        ("event", "\u00a0\u202f", None, None),
        ("event", "component", "\u2003", "record"),
        ("event", "component", "record", "\x85"),
    ],
)
def test_official_event_boundary_rejects_raw_out_of_domain_strings(
    event_type: str,
    component_id: str,
    affected_type: str | None,
    affected_id: str | None,
) -> None:
    with pytest.raises(AuditEventValueRejectedError):
        AuditEventInput(
            event_type=event_type,
            component_id=component_id,
            component_ip=None,
            affected_record_type=affected_type,
            affected_record_id=affected_id,
            occurred_at_utc=datetime.now(UTC),
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("http_method", "x" + (" " * 16)),
        ("object_verb", "x" + (" " * 64)),
        ("reason_code", "x" + (" " * 255)),
        ("request_id", "x" + (" " * 255)),
        ("http_method", "\t\n"),
        ("object_verb", "\u00a0\u202f"),
        ("reason_code", "\u2003"),
        ("request_id", "\x85"),
    ],
)
def test_official_payload_boundary_rejects_raw_out_of_domain_strings(
    key: str,
    value: str,
) -> None:
    with pytest.raises(AuditPayloadValueRejectedError):
        _normalize_payload({key: value})


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "event",
    [
        DirectAuditInput(event_type="x" + (" " * 128)),
        DirectAuditInput(component_id="x" + (" " * 255)),
        DirectAuditInput(
            affected_record_type="x" + (" " * 128),
            affected_record_id="record",
        ),
        DirectAuditInput(
            affected_record_type="record",
            affected_record_id="x" + (" " * 255),
        ),
        DirectAuditInput(payload=json.dumps({"http_method": "x" + (" " * 16)})),
        DirectAuditInput(payload=json.dumps({"object_verb": "x" + (" " * 64)})),
        DirectAuditInput(payload=json.dumps({"reason_code": "x" + (" " * 255)})),
        DirectAuditInput(payload=json.dumps({"request_id": "x" + (" " * 255)})),
        DirectAuditInput(payload=json.dumps({"reason_code": "\t\u00a0\u202f"})),
    ],
)
def test_tenant_sql_boundary_rejects_raw_out_of_domain_strings(
    app_database_url: str,
    event: DirectAuditInput,
) -> None:
    with psycopg.connect(app_database_url) as raw_connection:
        _set_context(raw_connection, uuid4(), uuid4())
        with pytest.raises(InvalidParameterValue):
            _tenant_append(raw_connection, event)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "event",
    [
        DirectAuditInput(event_type="x" + (" " * 128)),
        DirectAuditInput(payload=json.dumps({"reason_code": "x" + (" " * 255)})),
        DirectAuditInput(component_id="\u00a0\u202f"),
    ],
)
def test_system_sql_boundary_enforces_the_same_raw_string_domain(
    event: DirectAuditInput,
) -> None:
    with (
        psycopg.connect(_owner_database_url()) as raw_connection,
        pytest.raises(InvalidParameterValue),
    ):
        raw_connection.execute(
            "SELECT clinic_app.audit_append_system(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                event.event_type,
                event.component_id,
                None,
                event.affected_record_type,
                event.affected_record_id,
                datetime.now(UTC),
                event.payload,
                b"2" * 32,
            ),
        ).fetchone()


@pytest.mark.django_db(transaction=True)
def test_exact_maximum_strings_are_preserved_and_form_a_valid_chain() -> None:
    organization_id = uuid4()
    actor_user_id = uuid4()
    event = AuditEventInput(
        event_type=("e" * 127) + " ",
        component_id=("c" * 254) + " ",
        component_ip=None,
        affected_record_type=("t" * 127) + " ",
        affected_record_id=("i" * 254) + " ",
        occurred_at_utc=datetime.now(UTC),
    )
    payload = {
        "http_method": ("m" * 15) + " ",
        "object_verb": ("o" * 63) + " ",
        "reason_code": ("r" * 254) + " ",
        "request_id": ("q" * 254) + " ",
    }

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )
        seq = record_event(event, payload=payload)
        cursor.execute(
            "SELECT event_type, component_id, affected_record_type, "
            "affected_record_id, payload FROM clinic_app.audit_event_tenant "
            "WHERE seq = %s",
            [seq],
        )
        stored = cursor.fetchone()
        result = verify_chain(organization_id)

    assert stored is not None
    assert stored[:4] == (
        event.event_type,
        event.component_id,
        event.affected_record_type,
        event.affected_record_id,
    )
    assert json.loads(stored[4]) == payload
    assert result.valid is True


@pytest.mark.django_db(transaction=True)
def test_rejected_append_leaves_an_existing_chain_valid(
    app_database_url: str,
) -> None:
    organization_id = uuid4()
    actor_user_id = uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )
        record_event(_event(), payload={"reason_code": "valid"})

    with psycopg.connect(app_database_url) as raw_connection:
        _set_context(raw_connection, organization_id, actor_user_id)
        with pytest.raises(InvalidParameterValue):
            _tenant_append(
                raw_connection,
                DirectAuditInput(payload=json.dumps({"reason_code": " " * 256})),
            )

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )
        result = verify_chain(organization_id)

    assert result.row_count == 1
