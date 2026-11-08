"""0048: PostgreSQL refuses a forged TOTP step-up at the SQL level.

Each oracle acts as clinic_app with an entitled actor's own GUCs on a raw
clinic_app connection, so no Python guard is on the path. Before 0048 the
runtime role could read the device secret (``otp_totp_totpdevice.key``) and
stamp ``last_used_at`` directly, so ``identity_session_verify`` trusted a
session with no code and every SQL step-up consumer trusted that session. These
oracles are red on the base (the forgery succeeds, or the verifier the oracle
needs does not exist) and green on the fix, where the stamp also binds only in
the transaction that verified a genuine code. The session and passkey row
oracle is green on both: it pins that the verification row itself was already
resolver-only, so the passkey residual is exactly the two definers that trust
the Python WebAuthn check.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from apps.identity.models import Organization, Passkey
from apps.identity.session_inventory import session_hash
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from auth.passkey_test_support import registered_passkey
from database_urls import database_url_for_name
from identity.permission_support import owner_context
from otp_test_support import create_totp_device

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

_INSERT_CONFIRMED = (
    "INSERT INTO clinic_app.otp_totp_totpdevice "
    "(user_id, name, confirmed, key, step, t0, digits, tolerance, drift, "
    "last_t, throttling_failure_count, created_at, last_used_at) "
    "VALUES (%s, 'forged', true, '', 30, 0, 6, 1, 0, 0, 0, "
    "statement_timestamp(), statement_timestamp()) RETURNING id"
)


@contextmanager
def _app_connection(
    actor: UUID, *, tenant: UUID | None = None
) -> Iterator[psycopg.Connection[Any]]:
    """A raw clinic_app session with the actor's GUCs and no Python guard.

    autocommit keeps each statement its own transaction, so a refused forgery
    never aborts the ones that read its outcome.
    """
    url = database_url_for_name(
        os.environ["APP_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )
    external = psycopg.connect(url, autocommit=True)
    try:
        with external.cursor() as cursor:
            cursor.execute("SET search_path = clinic_app, public")
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, false)", [str(actor)]
            )
            if tenant is not None:
                cursor.execute(
                    "SELECT set_config('app.current_tenant', %s, false)", [str(tenant)]
                )
        yield external
    finally:
        external.close()


def _register_session(cursor: psycopg.Cursor[Any], actor: UUID) -> bytes:
    key = secrets.token_urlsafe(24)
    digest = session_hash(key)
    cursor.execute(
        "SELECT clinic_app.identity_session_register(%s, %s, %s, %s, %s)",
        [str(actor), digest, "", "other", timezone.now() + timedelta(hours=1)],
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] is not None
    return digest


def _try(cursor: psycopg.Cursor[Any], sql: str, params: list[object]) -> int | None:
    """Run one adversarial statement; a guard refusal returns None."""
    try:
        cursor.execute(sql, params)
    except psycopg.errors.DatabaseError:
        return None
    row = cursor.fetchone()
    return int(row[0]) if row is not None else 0


def _forge_session_verification(cursor: psycopg.Cursor[Any], actor: UUID) -> bytes:
    """Register a session and attempt both code-less device-verification paths."""
    digest = _register_session(cursor, actor)
    touched = _try(
        cursor,
        "UPDATE clinic_app.otp_totp_totpdevice "
        "SET last_used_at = statement_timestamp(), last_t = last_t "
        "WHERE user_id = %s AND confirmed RETURNING id",
        [str(actor)],
    )
    inserted = _try(cursor, _INSERT_CONFIRMED, [str(actor)])
    for device_id in (touched, inserted):
        if device_id is not None:
            _try(
                cursor,
                "SELECT clinic_app.identity_session_verify(%s, %s)",
                [digest, device_id],
            )
    return digest


def _scalar(cursor: psycopg.Cursor[Any]) -> object:
    row = cursor.fetchone()
    assert row is not None
    return row[0]


# -- the forgeable primitives are refused --------------------------------------


def test_runtime_cannot_write_device_verification_state(rbac_graph: RbacGraph) -> None:
    actor = rbac_graph.physician
    device = create_totp_device(actor, confirmed=True)
    with (
        _app_connection(actor) as external,
        external.cursor() as cursor,
        pytest.raises(psycopg.errors.DatabaseError),
    ):
        cursor.execute(
            "UPDATE clinic_app.otp_totp_totpdevice "
            "SET last_used_at = statement_timestamp(), last_t = last_t + 1 "
            "WHERE id = %s",
            [device.pk],
        )


def test_runtime_cannot_insert_a_confirmed_device(rbac_graph: RbacGraph) -> None:
    actor = rbac_graph.physician
    with (
        _app_connection(actor) as external,
        external.cursor() as cursor,
        pytest.raises(psycopg.errors.DatabaseError),
    ):
        cursor.execute(_INSERT_CONFIRMED, [str(actor)])


def test_runtime_cannot_read_the_totp_secret(rbac_graph: RbacGraph) -> None:
    actor = rbac_graph.physician
    device = create_totp_device(actor, confirmed=True)
    with _app_connection(actor) as external, external.cursor() as cursor:
        cursor.execute(
            "SELECT key FROM clinic_app.otp_totp_totpdevice WHERE id = %s", [device.pk]
        )
        # The secret used to live here in plaintext hex; it must be cleared. The
        # comparison is reduced to a flag so a failure never prints the seed.
        cleared = _scalar(cursor) == ""
        assert cleared, "otp_totp_totpdevice.key still holds the device seed"
        with pytest.raises(psycopg.errors.DatabaseError):
            cursor.execute(
                "SELECT secret FROM clinic_app.identity_totpsecret "
                "WHERE device_id = %s",
                [device.pk],
            )


@override_settings(WEBAUTHN_RP_ID="testserver", WEBAUTHN_ORIGIN="http://testserver")
def test_runtime_cannot_write_a_session_or_passkey_verification_row(
    rbac_graph: RbacGraph,
) -> None:
    # Given a genuine credential with passkey step-up enabled, so a stamped
    # session row would be the only missing piece of a passkey step-up.
    graph = rbac_graph
    actor = graph.clinic_admin
    registered_passkey(graph)
    device = create_totp_device(actor, confirmed=True)
    with owner_context(graph.organization_a):
        Organization.objects.filter(pk=graph.organization_a).update(
            passkey_step_up_enabled=True
        )
        credential = Passkey.objects.get(user_id=actor)
    with (
        _app_connection(actor, tenant=graph.organization_a) as external,
        external.cursor() as cursor,
    ):
        digest = _register_session(cursor, actor)
        # When clinic_app writes the verification row or the passkey use
        # directly, without the resolver definers.
        refusals: tuple[tuple[str, list[object], str], ...] = (
            (
                "UPDATE clinic_app.identity_sessionrecord "
                "SET verification_passkey_id = %s, "
                "verified_at = statement_timestamp() "
                "WHERE session_key_hash = %s",
                [credential.pk, digest],
                "23514",
            ),
            (
                "UPDATE clinic_app.identity_sessionrecord "
                "SET verification_device_id = %s, "
                "verified_at = statement_timestamp() "
                "WHERE session_key_hash = %s",
                [device.pk, digest],
                "23514",
            ),
            (
                "INSERT INTO clinic_app.identity_sessionrecord "
                "(id, user_id, session_key_hash, created_at, last_seen, ip_network, "
                "user_agent_family, expires_at, verification_passkey_id, "
                "verified_at) VALUES (gen_random_uuid(), %s, %s, now(), now(), "
                "'', 'other', now() + interval '1 hour', %s, now())",
                [str(actor), secrets.token_bytes(32), credential.pk],
                "42501",
            ),
            (
                "UPDATE clinic_app.identity_passkey "
                "SET sign_count = sign_count + 1, last_used = statement_timestamp() "
                "WHERE id = %s",
                [credential.pk],
                "42501",
            ),
        )
        for statement, parameters, sqlstate in refusals:
            with pytest.raises(psycopg.errors.DatabaseError) as failure:
                cursor.execute(statement, parameters)
            assert failure.value.sqlstate == sqlstate, statement
        # Then neither step-up kind holds for that session.
        cursor.execute(
            "SELECT clinic_app.identity_passkey_session_verified(%s, %s, 300) "
            "OR clinic_app.identity_session_verified(%s)",
            [digest, credential.pk, digest],
        )
        assert _scalar(cursor) is False


def test_session_verify_refused_without_a_real_code(rbac_graph: RbacGraph) -> None:
    actor = rbac_graph.physician
    create_totp_device(actor, confirmed=True)
    with _app_connection(actor) as external, external.cursor() as cursor:
        digest = _forge_session_verification(cursor, actor)
        cursor.execute("SELECT clinic_app.identity_session_verified(%s)", [digest])
        assert _scalar(cursor) is False


def test_a_genuine_use_cannot_verify_a_session_in_another_transaction(
    rbac_graph: RbacGraph,
) -> None:
    from apps.identity.otp import verify_totp_code  # fix-only  # noqa: PLC0415

    from otp_test_support import (  # noqa: PLC0415
        current_user_guc,
        runtime_role,
        token_for,
    )

    # Given the real user's genuine code, accepted and committed on its own, so
    # the device carries a fresh use for the next five minutes.
    actor = rbac_graph.physician
    device = create_totp_device(actor, confirmed=True)
    with runtime_role(), current_user_guc(actor):
        assert verify_totp_code(device.pk, token_for(device)) == 1
    # When a raw clinic_app connection registers a session of its own for that
    # user and asks for the stamp inside that window.
    with _app_connection(actor) as external, external.cursor() as cursor:
        digest = _register_session(cursor, actor)
        cursor.execute(
            "SELECT clinic_app.identity_session_verify(%s, %s)", [digest, device.pk]
        )
        stamped = _scalar(cursor)
        cursor.execute("SELECT clinic_app.identity_session_verified(%s)", [digest])
        verified = _scalar(cursor)
    # Then nothing binds: only the verifying transaction may stamp, once.
    assert stamped is False
    assert verified is False


def test_replayed_code_is_refused(rbac_graph: RbacGraph) -> None:
    from apps.identity.otp import verify_totp_code  # fix-only  # noqa: PLC0415

    from otp_test_support import (  # noqa: PLC0415
        current_user_guc,
        runtime_role,
        token_for,
    )

    actor = rbac_graph.physician
    device = create_totp_device(actor, confirmed=True)
    with runtime_role(), current_user_guc(actor):
        token = token_for(device)
        assert verify_totp_code(device.pk, token) == 1
    with runtime_role(), current_user_guc(actor):
        # Same time step, already consumed: last_t advanced past it.
        assert verify_totp_code(device.pk, token) != 1


# -- the B1 probe chain is refused for every SQL step-up consumer --------------


def test_b1_chain_refused_for_ai(rbac_graph: RbacGraph) -> None:
    actor = rbac_graph.physician
    create_totp_device(actor, confirmed=True)
    with _app_connection(actor, tenant=rbac_graph.organization_a) as external:
        with external.cursor() as cursor:
            digest = _forge_session_verification(cursor, actor)
        with external.cursor() as cursor:
            # agent_record_approval counts a vote only when this exact session
            # gate holds (apps/ai/migrations/_approval_attestation_sql.py:40);
            # a forged session fails it, so no forged vote is ever recorded. The
            # full writer path is exercised in tests/ai/test_vote_attestation.py,
            # now backed by this verifier through verified_request(persisted).
            cursor.execute(
                "SELECT clinic_app.identity_session_verified(%s) "
                "OR clinic_app.identity_passkey_session_verified(%s, NULL, 300)",
                [digest, digest],
            )
            assert _scalar(cursor) is False


def test_b1_chain_refused_for_billing(rbac_graph: RbacGraph) -> None:
    actor = rbac_graph.clinic_admin
    create_totp_device(actor, confirmed=True)
    with _app_connection(actor, tenant=rbac_graph.organization_a) as external:
        with external.cursor() as cursor:
            digest = _forge_session_verification(cursor, actor)
        with external.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_finance_session', %s, false)",
                [digest.hex()],
            )
            # billing_finance_session gates on identity_session_verified.
            with pytest.raises(psycopg.errors.DatabaseError):
                cursor.execute(
                    "SELECT clinic_app.billing_finance_session(%s)",
                    [rbac_graph.clinic_a],
                )


def test_b1_chain_refused_for_intake(rbac_graph: RbacGraph) -> None:
    actor = rbac_graph.clinic_admin
    create_totp_device(actor, confirmed=True)
    with _app_connection(actor, tenant=rbac_graph.organization_a) as external:
        with external.cursor() as cursor:
            digest = _forge_session_verification(cursor, actor)
            cursor.execute(
                "SELECT id FROM clinic_app.identity_sessionrecord "
                "WHERE session_key_hash = %s",
                [digest],
            )
            session_id = _scalar(cursor)
        with external.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_merge_session', %s, false)",
                [digest.hex()],
            )
            # intake_merge_authorized gates on identity_session_verified; assert
            # both the merge authority and the session gate it delegates to, so
            # the base's forged (verified) session is a clean red here.
            cursor.execute(
                "SELECT clinic_app.intake_merge_authorized(%s, %s), "
                "clinic_app.identity_session_verified(%s)",
                [rbac_graph.clinic_a, session_id, digest],
            )
            row = cursor.fetchone()
            assert row is not None
            authorized, verified = row
            assert authorized is False
            assert verified is False
