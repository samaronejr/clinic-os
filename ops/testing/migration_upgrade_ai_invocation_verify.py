"""Check native invocation graphs without receipt identifiers or row snapshots."""

from __future__ import annotations

import os
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import psycopg
from psycopg import errors, sql

from ops.testing.migration_upgrade_ai_fixture import (
    ARTIFACT_PURPOSE,
    CONFIGURATION,
    DIGEST,
    HISTORIES,
    INPUT_TEMPLATE,
    INVOCATION_LIMIT,
    METHODS,
    OUTPUT_TEMPLATE,
    OWNER_PURPOSES,
    PORTAL_CONTACT,
    PORTAL_INPUT,
    PORTAL_REPLY,
    PORTAL_TURN_STATES,
    RESERVED_MICROS,
    STATUSES,
    UNBOUND_PURPOSES,
)
from ops.testing.migration_upgrade_core_fixture import (
    CLINIC_NAME,
    ORGANIZATION_CNPJ,
    OWNER_USERNAME,
    PRACTITIONER_USERNAME,
)

if TYPE_CHECKING:
    from psycopg import Connection, Cursor

_UUID_VERSION = 4


def _uuid4(value: object) -> bool:
    return isinstance(value, UUID) and value.version == _UUID_VERSION


def _artifact(
    cursor: Cursor[tuple[object, ...]],
    envelope: object,
    expected: bytes,
    kek: str,
) -> bool:
    if not isinstance(envelope, bytes) or envelope == expected:
        return False
    cursor.execute(
        "SELECT clinic_app.tenant_decrypt(%s,%s,%s) = %s",
        [kek, ARTIFACT_PURPOSE, envelope, expected],
    )
    return cursor.fetchone() == (True,)


def _matches_artifact(
    admin: Connection[tuple[object, ...]],
    cursor: Cursor[tuple[object, ...]],
    envelope: object,
    expected: bytes,
    kek: str,
) -> bool:
    try:
        with admin.transaction():
            return _artifact(cursor, envelope, expected, kek)
    except errors.DatabaseError as error:
        if error.sqlstate not in {"TEN01", "TEN02", "TEN03", "39000"}:
            raise
        return False


def _context(cursor: Cursor[tuple[object, ...]]) -> bool:
    cursor.execute(
        "SELECT set_config('app.current_tenant',id::text,true) "
        "FROM clinic_app.identity_organization WHERE cnpj=%s",
        [ORGANIZATION_CNPJ],
    )
    return len(cursor.fetchall()) == 1


def _new_ids(keys: list[object], prior: set[object]) -> bool:
    return (
        all(_uuid4(value) for value in keys)
        and len(set(keys)) == len(keys)
        and prior.isdisjoint(keys)
    )


def _configuration_failures(cursor: Cursor[tuple[object, ...]]) -> list[str]:
    expected = {
        **CONFIGURATION,
        "input_micros": 0,
        "output_micros": 0,
        "cached_micros": 0,
        "audio_micros": 0,
        "timeout_seconds": 30,
    }
    cursor.execute(
        sql.SQL(
            "SELECT purpose,id,created_at,capability_key,{} "
            "FROM clinic_app.ai_modelconfiguration"
        ).format(
            sql.SQL(", ").join(
                sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(column))
                for column in expected
            )
        ),
        list(expected.values()),
    )
    rows = cursor.fetchall()
    failures = [
        f"AI configuration fixture: {purpose}.{column}"
        for purpose, _, _, _, *checks in rows
        for column, valid in zip(expected, checks, strict=True)
        if valid is not True
    ]
    failures.extend(
        f"AI configuration fixture: identity or capability {purpose}"
        for purpose, identity, created, capability, *_ in rows
        if not _uuid4(identity)
        or created is None
        or capability != ("asr" if purpose == "scribe_asr" else "llm_inference")
    )
    if len(rows) != len(HISTORIES) or {row[0] for row in rows} != {
        purpose for _, purpose in HISTORIES
    }:
        failures.append("AI configuration fixture: exact natural keys")
    return failures


def _portal_failures(cursor: Cursor[tuple[object, ...]], kek: str) -> list[str]:
    cursor.execute(
        """SELECT t.state, t.id, i.id, r.id, i.body, r.body,
            c.purpose='care' AND c.contact_fingerprint=%s
                AND e.organization_id=c.organization_id AND e.clinic_id=c.clinic_id
                AND clinic_app.tenant_decrypt(
                    %s,'comms.conversation.contact_address',c.contact_address) = %s,
            i.conversation_id=c.id AND i.direction='in'
                AND i.organization_id=c.organization_id
                AND i.created_at=c.last_activity_at
                AND i.event_id ~ '^[0-9a-f]{32}$'
                AND (r.id IS NULL OR
                    (r.conversation_id=c.id AND r.direction='out'
                     AND r.organization_id=c.organization_id
                     AND r.created_at=i.created_at)),
            t.created_at >= i.created_at,
            clinic_app.tenant_decrypt(%s,'comms.message.body',i.body) = %s,
            CASE WHEN r.id IS NOT NULL THEN
                clinic_app.tenant_decrypt(%s,'comms.message.body',r.body) = %s
            END,
            t.intent,t.outcome
        FROM clinic_app.ai_frontdeskturn t
        JOIN clinic_app.ai_frontdeskthread d ON d.id=t.thread_id
        JOIN clinic_app.comms_conversation c ON c.id=d.conversation_id
        JOIN clinic_app.intake_patientclinicenrollment e ON e.id=c.enrollment_id
        JOIN clinic_app.comms_message i ON i.id=t.inbound_id
        LEFT JOIN clinic_app.comms_message r ON r.id=t.reply_id
        WHERE c.channel='portal'""",
        [
            DIGEST,
            kek,
            PORTAL_CONTACT.encode(),
            kek,
            PORTAL_INPUT.encode(),
            kek,
            PORTAL_REPLY.encode(),
        ],
    )
    rows = cursor.fetchall()
    failures = []
    if len(rows) != len(PORTAL_TURN_STATES) or {r[0] for r in rows} != set(
        PORTAL_TURN_STATES
    ):
        failures.append("AI portal fixture: exact natural keys")
    prior: set[object] = set()
    ciphertexts: set[object] = set()
    for row in rows:
        (
            state,
            turn,
            inbound,
            reply,
            input_bytes,
            reply_bytes,
            contact,
            links,
            order,
            input_ok,
            reply_ok,
            intent,
            outcome,
        ) = row
        done = state == "done"
        keys = [turn, inbound, *([reply] if done else [])]
        if not (
            _new_ids(keys, prior)
            and contact is True
            and links is True
            and order is True
            and input_ok is True
            and input_bytes not in ciphertexts
            and (reply_ok, intent, outcome)
            == ((True, "greeting", "answered") if done else (None, "", ""))
        ):
            failures.append(f"AI portal fixture: declared turn {state}")
        prior.update(keys)
        ciphertexts.update((input_bytes, reply_bytes))
    return failures


def verify_invocation_fixture() -> list[str]:
    """Check declared invocations, artifact values, and operation relationships."""
    kek = (
        (Path(os.environ["CLINIC_SECRET_DIR"]) / "tenant-kek.secret")
        .read_text(encoding="ascii")
        .strip()
    )
    expected_keys = set(product((purpose for _, purpose in HISTORIES), STATUSES))
    seen: set[tuple[str, str]] = set()
    ids: set[object] = set()
    ciphertexts: set[object] = set()
    failures = []
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if not _context(cursor):
            return ["AI invocation fixture: organization natural key"]
        failures.extend(_configuration_failures(cursor))
        failures.extend(_portal_failures(cursor, kek))
        cursor.execute(
            """SELECT i.purpose,i.status,i.id,s.id,t.id,o.id,o.idempotency_key,
                u.username,c.purpose,i0.name=%s
                    AND i0.organization_id=i.organization_id,
                (i.config_version,i.prompt_sha256,i.limit_tokens_in,i.limit_tokens_out,
                 i.limit_audio_seconds,i.reserved_micros) = (1,%s,%s,%s,%s,%s)
                    AND (i.tokens_in,i.tokens_out,i.tokens_cached,i.audio_seconds,
                         i.cost_micros,i.cost_minor,i.latency_ms,i.reason_code,i.effect_xid)
                         = (0,0,0,0,0,0,0,'','') AND i.rollout_approval_id IS NULL,
                (o.kind,o.channel,o.payload_digest,o.provider,o.subject_type)
                    = ('action','',%s,'ai-gateway','ai.invocation')
                    AND o.subject_id=i.id AND o.actor_id=i.user_id
                    AND o.organization_id=i.organization_id AND o.clinic_id=i.clinic_id,
                s.method,s.content,t.content,
                (s.kind,s.retention_class,s.purpose)
                    = ('input','transient_input',i.purpose)
                    AND s.user_id=i.user_id AND s.organization_id=i.organization_id
                    AND s.clinic_id=i.clinic_id AND s.invocation_id IS NULL
                    AND s.purged_at IS NULL,
                (t.kind,t.retention_class,t.purpose,t.method)
                    = ('output','review_draft',i.purpose,s.method)
                    AND t.user_id=i.user_id AND t.organization_id=i.organization_id
                    AND t.clinic_id=i.clinic_id AND t.purged_at IS NULL
                    AND t.patient_enrollment_id
                        IS NOT DISTINCT FROM s.patient_enrollment_id,
                s.patient_enrollment_id IS NOT NULL AND
                    (e.organization_id,e.clinic_id)=(i.organization_id,i.clinic_id),
                c.created_at <= s.created_at
                    AND s.created_at <= o.created_at AND o.created_at <= i.created_at
                    AND s.expires_at > i.created_at
                    AND (t.id IS NULL OR
                         (i.created_at <= t.created_at AND t.expires_at=s.expires_at)),
                i.settled_at IS NOT NULL
                    AND c.created_at <= i.settled_at AND i.settled_at <= s.created_at
            FROM clinic_app.ai_aiinvocation i
            JOIN clinic_app.ai_aiartifact s ON s.id=i.inputs_ref_id
            LEFT JOIN clinic_app.ai_aiartifact t ON t.invocation_id=i.id
            JOIN clinic_app.identity_user u ON u.id=i.user_id
            JOIN clinic_app.identity_clinic i0 ON i0.id=i.clinic_id
            JOIN clinic_app.ai_modelconfiguration c ON c.id=i.configuration_id
            JOIN clinic_app.comms_integrationoperation o ON o.id=i.operation_id
            LEFT JOIN clinic_app.intake_patientclinicenrollment e
                ON e.id=s.patient_enrollment_id""",
            [
                CLINIC_NAME,
                DIGEST,
                INVOCATION_LIMIT,
                INVOCATION_LIMIT,
                INVOCATION_LIMIT,
                RESERVED_MICROS,
                DIGEST,
            ],
        )
        rows = cursor.fetchall()
        for row in rows:
            (
                raw_purpose,
                raw_status,
                invocation,
                source,
                output,
                operation,
                command,
                username,
                configuration,
                clinic,
                limits,
                link,
                method,
                source_bytes,
                output_bytes,
                source_shape,
                output_shape,
                enrolled,
                chronology,
                settled,
            ) = row
            purpose, status = str(raw_purpose), str(raw_status)
            key = (purpose, status)
            if key not in expected_keys or key in seen:
                failures.append(
                    "AI invocation fixture: unexpected or duplicate natural key"
                )
                continue
            seen.add(key)
            succeeded = status == "succeeded"
            keys = [
                invocation,
                source,
                operation,
                command,
                *([output] if succeeded else []),
            ]
            if not _new_ids(keys, ids):
                failures.append(f"AI invocation fixture: generated UUID {key}")
            ids.update(keys)
            actor = (
                OWNER_USERNAME if purpose in OWNER_PURPOSES else PRACTITIONER_USERNAME
            )
            if not (
                link is True
                and clinic is True
                and configuration == purpose
                and username == actor
                and enrolled is (purpose not in UNBOUND_PURPOSES)
            ):
                failures.append(f"AI invocation fixture: foreign-key target {key}")
            failures.extend(
                f"AI invocation fixture: {reason} {key}"
                for reason, valid in (
                    (
                        "defined values",
                        (
                            limits is True
                            and source_shape is True
                            and method == METHODS.get(purpose, "complete")
                            and (
                                (output_shape is True) if succeeded else output is None
                            )
                        ),
                    ),
                    ("timestamp order", chronology is True and settled is succeeded),
                )
                if not valid
            )

            fields = [(source_bytes, INPUT_TEMPLATE)]
            if succeeded:
                fields.append((output_bytes, OUTPUT_TEMPLATE))
            for envelope, template in fields:
                valid = _matches_artifact(
                    admin,
                    cursor,
                    envelope,
                    template.format(purpose=purpose, status=status).encode(),
                    kek,
                )
                if not valid or envelope in ciphertexts:
                    failures.append(f"AI invocation fixture: ciphertext {key}")
                ciphertexts.add(envelope)

        cursor.execute(
            "SELECT (SELECT count(*) FROM clinic_app.ai_aiinvocation),"
            "(SELECT count(*) FROM clinic_app.ai_aiartifact),"
            "(SELECT count(*) FROM clinic_app.comms_integrationoperation "
            " WHERE subject_type='ai.invocation')"
        )
        if seen != expected_keys or cursor.fetchone() != (
            len(expected_keys),
            len(expected_keys) + len(HISTORIES),
            len(expected_keys),
        ):
            failures.append("AI invocation fixture: exact row counts")
    return failures
