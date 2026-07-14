from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.audit.models import SYSTEM_ORG_ID
from apps.audit.services import (
    AuditEventInput,
    AuditEventValueRejectedError,
    AuditPayloadValueRejectedError,
    _normalize_payload,
    _record_system_event,
    record_event,
    verify_chain,
)
from django.db import connection, transaction

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.audit.services import AuditPayloadInputValue


class _AuditAppender(Protocol):
    def __call__(
        self,
        event: AuditEventInput,
        *,
        payload: Mapping[str, AuditPayloadInputValue],
    ) -> int: ...


_UNPERSISTABLE_TEXT = (
    "valid\x00tail",
    "valid\ud800tail",
    "valid\udffftail",
)


def _event(
    *,
    event_type: str = "audit.synthetic",
    component_id: str = "unicode-boundary-test",
    affected_record_type: str | None = None,
    affected_record_id: str | None = None,
) -> AuditEventInput:
    return AuditEventInput(
        event_type=event_type,
        component_id=component_id,
        component_ip=None,
        affected_record_type=affected_record_type,
        affected_record_id=affected_record_id,
        occurred_at_utc=datetime.now(UTC),
    )


@pytest.mark.parametrize("value", _UNPERSISTABLE_TEXT)
def test_event_boundary_rejects_unpersistable_unicode(value: str) -> None:
    with pytest.raises(AuditEventValueRejectedError) as event_type_error:
        _event(event_type=value)
    assert event_type_error.value.field == "event_type"

    with pytest.raises(AuditEventValueRejectedError) as component_error:
        _event(component_id=value)
    assert component_error.value.field == "component_id"

    with pytest.raises(AuditEventValueRejectedError) as affected_type_error:
        _event(affected_record_type=value, affected_record_id="record-1")
    assert affected_type_error.value.field == "affected_record"

    with pytest.raises(AuditEventValueRejectedError) as affected_id_error:
        _event(affected_record_type="record", affected_record_id=value)
    assert affected_id_error.value.field == "affected_record"


@pytest.mark.parametrize("value", _UNPERSISTABLE_TEXT)
def test_payload_boundary_rejects_unpersistable_unicode(value: str) -> None:
    with pytest.raises(AuditPayloadValueRejectedError) as exc_info:
        _normalize_payload({"reason_code": value})

    assert exc_info.value.key == "reason_code"


@pytest.mark.parametrize(
    "appender",
    [
        pytest.param(record_event, id="tenant"),
        pytest.param(_record_system_event, id="system"),
    ],
)
def test_payload_rejection_happens_before_database_access(
    appender: _AuditAppender,
) -> None:
    with (
        patch("apps.audit.services.connection.cursor") as cursor,
        pytest.raises(AuditPayloadValueRejectedError),
    ):
        appender(_event(), payload={"reason_code": _UNPERSISTABLE_TEXT[0]})

    cursor.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_rejected_tenant_text_leaves_the_valid_chain_unchanged() -> None:
    organization_id = uuid4()
    actor_user_id = uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )
        valid_seq = record_event(_event(), payload={"reason_code": "valid"})
        for value in _UNPERSISTABLE_TEXT:
            with pytest.raises(AuditPayloadValueRejectedError):
                record_event(_event(), payload={"reason_code": value})
        result = verify_chain(organization_id)

    assert result.valid is True
    assert result.row_count == 1
    assert result.last_seq == valid_seq


@pytest.mark.django_db(transaction=True)
def test_rejected_system_text_leaves_the_valid_chain_unchanged() -> None:
    valid_seq = _record_system_event(
        _event(),
        payload={"reason_code": "valid"},
    )
    for value in _UNPERSISTABLE_TEXT:
        with pytest.raises(AuditPayloadValueRejectedError):
            _record_system_event(_event(), payload={"reason_code": value})

    result = verify_chain()

    assert result.valid is True
    assert result.organization_id == SYSTEM_ORG_ID
    assert result.row_count == 1
    assert result.last_seq == valid_seq
