from dataclasses import replace
from datetime import UTC, datetime
from inspect import signature
from ipaddress import IPv4Address, IPv4Network, IPv6Address
from uuid import uuid4

import pytest
from apps.audit.services import (
    AuditEventInput,
    AuditEventValueRejectedError,
    record_event,
    verify_chain,
)
from django.db import connection, transaction


def _event() -> AuditEventInput:
    return AuditEventInput(
        event_type="audit.synthetic",
        component_id="component-ip-test",
        component_ip=None,
        affected_record_type=None,
        affected_record_id=None,
        occurred_at_utc=datetime.now(UTC),
    )


def _runtime_event(
    component_ip: str | bytes | int | IPv4Network,
) -> AuditEventInput:
    bound = signature(AuditEventInput).bind(
        event_type="audit.synthetic",
        component_id="component-ip-test",
        component_ip=component_ip,
        affected_record_type=None,
        affected_record_id=None,
        occurred_at_utc=datetime.now(UTC),
    )
    return AuditEventInput(*bound.args, **bound.kwargs)


@pytest.mark.parametrize(
    "component_ip",
    [
        "192.0.2.1/32",
        "2001:0db8:0000:0000:0000:0000:0000:0001",
        b"\xc0\x00\x02\x01",
        3221225985,
        IPv4Network("192.0.2.0/24"),
    ],
)
def test_event_rejects_non_address_component_ip_runtime_values(
    component_ip: str | bytes | int | IPv4Network,
) -> None:
    # Given: input that could be normalized differently by ipaddress or PostgreSQL
    # When: a dynamic caller reaches the constructor without static type checking
    with pytest.raises(AuditEventValueRejectedError) as exc_info:
        _runtime_event(component_ip)

    # Then: the semantic boundary rejects it before hashing or SQL
    assert exc_info.value.field == "component_ip"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "component_ip",
    [IPv4Address("192.0.2.1"), IPv6Address("2001:db8::1")],
)
def test_address_component_ip_persists_and_immediately_verifies(
    component_ip: IPv4Address | IPv6Address,
) -> None:
    # Given: a valid address object and trusted tenant/actor transaction context
    organization_id = uuid4()
    actor_user_id = uuid4()
    event = replace(_event(), component_ip=component_ip)

    # When: the supported service appends and immediately verifies the same chain
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(actor_user_id)],
        )
        seq = record_event(event, payload={"reason_code": "component-ip-valid"})
        cursor.execute(
            "SELECT host(component_ip) FROM clinic_app.audit_event_tenant "
            "WHERE seq = %s",
            [seq],
        )
        stored = cursor.fetchone()
        verification = verify_chain(organization_id)

    # Then: storage keeps the canonical host and verification accepts its hash
    assert stored == (str(component_ip),)
    assert verification.row_count == 1
    assert verification.last_seq == seq
