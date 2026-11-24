"""Check the overlap graph against declared inbox keys before behavior probes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

import psycopg

from ops.testing.migration_upgrade_core_fixture import CLINIC_NAME, ORGANIZATION_CNPJ
from ops.testing.migration_upgrade_inbox_fixture import (
    ACCOUNTS,
    CASES,
    INBOUND_BODY,
    METERED_DELIVERY_CASES,
    OUTBOUND_BODY,
    PROVIDER_METER_CASES,
    STATUS_RECEIPTS,
    contact_address,
)

if TYPE_CHECKING:
    from psycopg import Cursor

_UUID_VERSION = 4
_IDENTITIES_PER_CASE = 4


class InboxFixtureResult(NamedTuple):
    """Independently resolved inbox probe keys and assertion failures."""

    failures: list[str]
    cases: dict[str, dict[str, str]]


def _effects(
    cursor: Cursor[tuple[object, ...]], cases: dict[str, dict[str, str]]
) -> list[str]:
    failures = []
    cursor.execute(
        """SELECT pa.channel,e.kind,count(*)
        FROM clinic_app.comms_inboundevent e
        JOIN clinic_app.comms_provideraccount pa
            ON pa.id=e.provider_account_id AND pa.organization_id=e.organization_id
        GROUP BY pa.channel,e.kind"""
    )
    expected = {
        (channel, "status", count) for channel, count in STATUS_RECEIPTS.items()
    }
    expected.update(
        (channel, "message", sum(ch == channel for _, ch, _ in CASES))
        for channel in ACCOUNTS
    )
    if set(cursor.fetchall()) != expected:
        failures.append("inbox fixture: receipt counts by account natural key")
    cursor.execute(
        """SELECT m.message_id::text,m.source,m.category,m.billable,m.pricing_type,
               m.recorded_at >= message.created_at
        FROM clinic_app.comms_messagemeter m
        JOIN clinic_app.comms_message message
          ON message.id=m.message_id AND message.organization_id=m.organization_id"""
    )
    expected_meters: set[tuple[str, str, str, bool | None, str, bool]] = {
        (keys["message"], "expected", "service", None, "", True)
        for keys in cases.values()
        if keys["channel"] == "whatsapp"
    }
    expected_meters.update(
        (cases[case]["message"], "provider", "utility", True, "regular", True)
        for case in PROVIDER_METER_CASES & cases.keys()
    )
    if set(cursor.fetchall()) != expected_meters:
        failures.append("inbox fixture: declared expected and provider meter facts")
    cursor.execute(
        """SELECT u.source_ref::text,u.meter,u.quantity,u.occurred_at=m.created_at
        FROM clinic_app.subscriptions_usageevent u
        JOIN clinic_app.comms_message m
          ON m.id=u.source_ref AND m.organization_id=u.organization_id
        WHERE u.meter='messages'"""
    )
    if set(cursor.fetchall()) != {
        (keys["message"], "messages", 1, True)
        for case, keys in cases.items()
        if case in METERED_DELIVERY_CASES
    }:
        failures.append("inbox fixture: declared outbound usage sources")
    cursor.execute(
        """WITH expected AS (
            SELECT organization_id,
                   date_trunc('month',occurred_at AT TIME ZONE 'UTC')::date AS period,
                   sum(quantity) AS quantity
            FROM clinic_app.subscriptions_usageevent WHERE meter='messages'
            GROUP BY organization_id,period
        )
        SELECT count(*) FROM expected e
        FULL JOIN (SELECT * FROM clinic_app.subscriptions_usagerollup
                   WHERE meter='messages') r USING (organization_id,period)
        WHERE e.quantity IS DISTINCT FROM r.quantity"""
    )
    if cursor.fetchone() != (0,):
        failures.append("inbox fixture: usage rollups by tenant and UTC month")
    return failures


def _inbox_rows(
    cursor: Cursor[tuple[object, ...]], kek: str
) -> list[tuple[object, ...]]:
    cursor.execute(
        """SELECT c.channel,c.id,pa.provider,pa.external_account_id,
            clinic_app.tenant_decrypt(%s,'comms.conversation.contact_address',
                                     c.contact_address),
            mi.id,mo.id,mo.command_id,mi.event_id,
            clinic_app.tenant_decrypt(%s,'comms.message.body',mi.body) = %s,
            clinic_app.tenant_decrypt(%s,'comms.message.body',mo.body) = %s,
            clinic_app.tenant_decrypt(%s,'comms.message.body',mo.body) = mo.body,
            mi.created_at <= mo.created_at,
            (pa.organization_id,pa.clinic_id,pa.channel) =
                (c.organization_id,c.clinic_id,c.channel) AND cc.name=%s
                AND cc.organization_id=c.organization_id
                AND p.purpose='messaging_ingest'
                AND (p.organization_id,p.clinic_id)=(c.organization_id,c.clinic_id)
                AND mi.organization_id=c.organization_id
                AND mo.organization_id=c.organization_id
                AND EXISTS (
                    SELECT 1 FROM clinic_app.identity_userclinicrole r
                    WHERE r.user_id=mo.sent_by_id AND r.role='clinic_admin'
                      AND (r.organization_id,r.clinic_id)
                          = (c.organization_id,c.clinic_id)
                ),
            (SELECT count(*) FROM clinic_app.comms_integrationoperation o
             WHERE o.subject_type='comms.message' AND o.subject_id=mo.id
               AND (o.organization_id,o.clinic_id,o.channel)=
                   (c.organization_id,c.clinic_id,c.channel)),
            (SELECT count(*) FROM clinic_app.comms_integrationoperation o
             WHERE o.subject_type='comms.message' AND o.subject_id=mo.id
               AND o.idempotency_key=mo.command_id)
        FROM clinic_app.comms_conversation c
        JOIN clinic_app.identity_clinic cc ON cc.id=c.clinic_id
        JOIN clinic_app.comms_provideraccount pa ON pa.id=c.provider_account_id
        JOIN clinic_app.identity_serviceprincipal p ON p.id=pa.principal_id
        JOIN clinic_app.comms_message mi
            ON mi.conversation_id=c.id AND mi.direction='in'
        JOIN clinic_app.comms_message mo
            ON mo.conversation_id=c.id AND mo.direction='out'
        WHERE c.organization_id=current_setting('app.current_tenant')::uuid""",
        [kek, kek, INBOUND_BODY, kek, OUTBOUND_BODY, kek, CLINIC_NAME],
    )
    return cursor.fetchall()


def verify_inbox_fixture() -> InboxFixtureResult:
    """Resolve histories from decrypted fixture contacts and verify their shape."""
    kek = (
        (Path(os.environ["CLINIC_SECRET_DIR"]) / "tenant-kek.secret")
        .read_text(encoding="ascii")
        .strip()
    )
    declared = {
        (channel, contact_address(channel, ordinal)): (case, ordinal, attempts)
        for ordinal, (case, channel, attempts) in enumerate(CASES, 1)
    }
    failures = []
    cases = {}
    seen = set()
    identities: set[object] = set()
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute(
            "SELECT set_config('app.current_tenant',id::text,true) "
            "FROM clinic_app.identity_organization WHERE cnpj=%s",
            [ORGANIZATION_CNPJ],
        )
        if len(cursor.fetchall()) != 1:
            return InboxFixtureResult(["inbox fixture: organization natural key"], {})
        for row in _inbox_rows(cursor, kek):
            (
                channel,
                conversation,
                provider,
                external,
                contact,
                inbound,
                outbound,
                command,
                event,
                inbound_body,
                outbound_body,
                plaintext,
                chronology,
                relationships,
                count,
                original,
            ) = row
            key = (channel, contact)
            if key in seen or key not in declared:
                failures.append("inbox fixture: unexpected or duplicate contact key")
                continue
            seen.add(key)
            case, ordinal, attempts = declared[key]
            cases[case] = {
                "message": str(outbound),
                "conversation": str(conversation),
                "channel": str(channel),
            }
            keys = {conversation, inbound, outbound, command}
            failures.extend(
                f"inbox fixture: {case} {reason}"
                for reason, valid in (
                    ("account", ACCOUNTS[str(channel)] == (provider, external)),
                    ("foreign-key target", relationships is True and original == 1),
                    ("attempt count", count == attempts),
                    ("ordered messages", chronology is True),
                    (
                        "message ciphertext",
                        inbound_body is True
                        and outbound_body is True
                        and plaintext is False,
                    ),
                    ("inbound source", str(event).startswith(f"upgrade.{ordinal}.")),
                    (
                        "generated UUIDs",
                        len(keys) == _IDENTITIES_PER_CASE
                        and identities.isdisjoint(keys)
                        and all(
                            isinstance(key, UUID) and key.version == _UUID_VERSION
                            for key in keys
                        ),
                    ),
                )
                if not valid
            )
            identities.update(keys)
        cursor.execute(
            "SELECT (SELECT count(*) FROM clinic_app.comms_conversation "
            " WHERE channel IN ('whatsapp','email','sms')),"
            "(SELECT count(*) FROM clinic_app.comms_message m "
            " JOIN clinic_app.comms_conversation c ON c.id=m.conversation_id "
            " WHERE c.channel IN ('whatsapp','email','sms')),"
            "(SELECT count(*) FROM clinic_app.comms_provideraccount),"
            "(SELECT count(*) FROM clinic_app.comms_integrationoperation "
            " WHERE subject_type='comms.message')"
        )
        if seen != declared.keys() or cursor.fetchone() != (
            len(CASES),
            2 * len(CASES),
            len(ACCOUNTS),
            sum(attempts for _, _, attempts in CASES),
        ):
            failures.append("inbox fixture: exact natural keys and row counts")
        failures.extend(_effects(cursor, cases))
    return InboxFixtureResult(failures, cases)
