"""Check the overlap graph against declared inbox keys before behavior probes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import psycopg

from ops.testing.migration_upgrade_core_fixture import CLINIC_NAME, ORGANIZATION_CNPJ
from ops.testing.migration_upgrade_inbox_fixture import (
    ACCOUNTS,
    CASES,
    INBOUND_BODY,
    OUTBOUND_BODY,
    contact_address,
)

if TYPE_CHECKING:
    from psycopg import Cursor

_UUID_VERSION = 4
_IDENTITIES_PER_CASE = 4


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


def verify_inbox_fixture() -> list[str]:
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
            return ["inbox fixture: organization natural key"]
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
    return failures
