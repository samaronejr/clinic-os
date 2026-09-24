from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from hashlib import sha256
from typing import TYPE_CHECKING, ClassVar, Protocol
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.audit.models import SYSTEM_ORG_ID
from apps.audit.services import (
    AuditEventInput,
    AuditEventValueRejectedError,
    AuditTrustedContext,
    _content_hash,
    _record_system_event,
    record_event,
    verify_chain,
)
from django.db import connection, transaction

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.audit.services import AuditPayloadInputValue

_ZERO_HASH = b"\x00" * 32


class _AuditAppender(Protocol):
    def __call__(
        self,
        event: AuditEventInput,
        *,
        payload: Mapping[str, AuditPayloadInputValue],
    ) -> int: ...


class _AlternateDateTime(datetime):
    astimezone_called: ClassVar[bool] = False

    def astimezone(self, tz: tzinfo | None = None) -> _AlternateDateTime:
        type(self).astimezone_called = True
        return _AlternateDateTime(2099, 1, 1, 0, 0, tzinfo=UTC)


class _CountingTimezone(tzinfo):
    utcoffset_calls: ClassVar[int] = 0

    def utcoffset(self, dt: datetime | None) -> timedelta:
        type(self).utcoffset_calls += 1
        return timedelta(hours=2)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "COUNTED+02"


class _NaiveLikeTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None

    def tzname(self, dt: datetime | None) -> None:
        return None


def _event(occurred_at_utc: datetime) -> AuditEventInput:
    return AuditEventInput(
        event_type="audit.synthetic",
        component_id="datetime-test",
        component_ip=None,
        affected_record_type=None,
        affected_record_id=None,
        occurred_at_utc=occurred_at_utc,
    )


def _audit_row_count() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM clinic_app.audit_event")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize(
    "timestamp",
    [
        pytest.param(
            datetime(2026, 7, 14, 12, 34, 56),  # noqa: DTZ001 - rejection input
            id="no-tzinfo",
        ),
        pytest.param(
            datetime(2026, 7, 14, 12, 34, 56, tzinfo=_NaiveLikeTimezone()),
            id="none-offset",
        ),
    ],
)
def test_unaware_datetime_is_rejected_at_event_boundary(timestamp: datetime) -> None:
    with pytest.raises(AuditEventValueRejectedError) as exc_info:
        _event(timestamp)

    assert exc_info.value.field == "occurred_at_utc"


def test_custom_timezone_is_observed_once_before_canonical_hashing() -> None:
    # Given: an exact datetime carrying a custom but valid aware timezone
    source_timezone = _CountingTimezone()
    timestamp = datetime(2026, 7, 14, 12, 34, 56, 123456, tzinfo=source_timezone)
    _CountingTimezone.utcoffset_calls = 0

    # When: the boundary constructs the event and canonical hashing consumes it
    event = _event(timestamp)
    _content_hash(
        event,
        AuditTrustedContext(uuid4(), None),
        {"reason_code": "counted-timezone"},
    )

    # Then: custom timezone behavior is consumed once and replaced by exact UTC
    assert _CountingTimezone.utcoffset_calls == 1
    assert type(event.occurred_at_utc) is datetime
    assert event.occurred_at_utc == datetime(
        2026,
        7,
        14,
        10,
        34,
        56,
        123456,
        tzinfo=UTC,
    )
    assert event.occurred_at_utc.tzinfo is UTC


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "appender",
    [
        pytest.param(record_event, id="tenant"),
        pytest.param(_record_system_event, id="system"),
    ],
)
def test_datetime_subclass_is_rejected_before_hash_or_cursor(
    appender: _AuditAppender,
) -> None:
    # Given: a constructible datetime subclass with an alternate UTC conversion
    now = datetime.now(UTC)
    subclass_value = _AlternateDateTime(
        now.year,
        now.month,
        now.day,
        now.hour,
        now.minute,
        now.second,
        now.microsecond,
        tzinfo=UTC,
    )
    _AlternateDateTime.astimezone_called = False
    row_count_before = _audit_row_count()

    # When: either supported path receives the dynamic runtime subtype
    with (
        patch(
            "apps.audit.services._content_hash",
            side_effect=AssertionError("hash boundary reached"),
        ) as content_hash,
        patch(
            "apps.audit.services.connection.cursor",
            side_effect=AssertionError("cursor boundary reached"),
        ) as cursor_factory,
        pytest.raises(AuditEventValueRejectedError) as exc_info,
    ):
        appender(
            _event(subclass_value),
            payload={"reason_code": "datetime-subclass"},
        )

    # Then: construction rejects without observing subtype behavior or appending
    assert exc_info.value.field == "occurred_at_utc"
    assert _AlternateDateTime.astimezone_called is False
    content_hash.assert_not_called()
    cursor_factory.assert_not_called()
    assert _audit_row_count() == row_count_before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "source_timezone",
    [
        pytest.param(UTC, id="utc"),
        pytest.param(timezone(timedelta(hours=5, minutes=30)), id="fixed-offset"),
    ],
)
def test_tenant_timestamp_is_normalized_once_and_immediately_verifies(
    source_timezone: tzinfo,
) -> None:
    # Given: an ordinary aware timestamp and trusted tenant context
    organization_id = uuid4()
    actor_user_id = uuid4()
    raw_timestamp = (
        datetime.now(UTC).replace(microsecond=123456).astimezone(source_timezone)
    )
    event = _event(raw_timestamp)
    payload = {"reason_code": "datetime-tenant-valid"}

    # When: the supported tenant service stores and verifies the event
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )
        seq = record_event(event, payload=payload)
        cursor.execute(
            "SELECT occurred_at_utc, prev_hash, curr_hash "
            "FROM clinic_app.audit_event_tenant WHERE seq = %s",
            [seq],
        )
        stored = cursor.fetchone()
        verification = verify_chain(organization_id)

    # Then: one exact UTC value feeds canonical hashing and timestamptz storage
    assert type(event.occurred_at_utc) is datetime
    assert event.occurred_at_utc.tzinfo is UTC
    assert event.occurred_at_utc == raw_timestamp.astimezone(UTC)
    assert stored is not None
    assert stored[0] == event.occurred_at_utc
    assert bytes(stored[1]) == _ZERO_HASH
    content_hash = _content_hash(
        event,
        AuditTrustedContext(organization_id, actor_user_id),
        payload,
    )
    assert bytes(stored[2]) == sha256(content_hash + _ZERO_HASH).digest()
    assert verification.row_count == 1
    assert verification.last_seq == seq


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "source_timezone",
    [
        pytest.param(UTC, id="utc"),
        pytest.param(timezone(timedelta(hours=-3)), id="fixed-offset"),
    ],
)
def test_system_timestamp_is_normalized_once_and_immediately_verifies(
    source_timezone: tzinfo,
) -> None:
    # Given: an ordinary aware timestamp and an explicit actor-free owner context
    raw_timestamp = (
        datetime.now(UTC).replace(microsecond=654321).astimezone(source_timezone)
    )
    event = _event(raw_timestamp)
    payload = {"reason_code": "datetime-system-valid"}

    # When: the owner-only service stores and verifies the system event
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_user_id', '', true)")
        seq = _record_system_event(event, payload=payload)
        cursor.execute(
            "SELECT occurred_at_utc, prev_hash, curr_hash "
            "FROM clinic_app.audit_event WHERE organization_id = %s AND seq = %s",
            [SYSTEM_ORG_ID, seq],
        )
        stored = cursor.fetchone()
        verification = verify_chain()

    # Then: one exact UTC value feeds canonical hashing and timestamptz storage
    assert type(event.occurred_at_utc) is datetime
    assert event.occurred_at_utc.tzinfo is UTC
    assert event.occurred_at_utc == raw_timestamp.astimezone(UTC)
    assert stored is not None
    assert stored[0] == event.occurred_at_utc
    assert bytes(stored[1]) == _ZERO_HASH
    content_hash = _content_hash(
        event,
        AuditTrustedContext(SYSTEM_ORG_ID, None),
        payload,
    )
    assert bytes(stored[2]) == sha256(content_hash + _ZERO_HASH).digest()
    assert verification.row_count == 1
    assert verification.last_seq == seq
