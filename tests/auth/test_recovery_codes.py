"""Actual-role once-shown recovery and authenticated database-clock reset proof."""

from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core.integration import execute_operation, receive_provider_callback
from apps.identity.invitations import EMAIL_PROVIDER
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.models import AuthenticatorReset, RecoveryCode, SessionRecord
from apps.identity.notifications import (
    SIGNATURE_HEADER,
    register_identity_notifications,
)
from apps.identity.recovery import (
    execute_authenticator_reset,
    issue_recovery_codes,
    recover_authenticator,
    request_authenticator_reset,
)
from apps.identity.session_inventory import session_hash
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import Client
from django.utils.crypto import salted_hmac

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from otp_test_support import create_totp_device, runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_recovery_codes_are_once_shown_hash_only_and_one_use(
    rbac_graph: RbacGraph,
) -> None:
    client = verified_client(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        issued = issue_recovery_codes(
            clinic_id=rbac_graph.clinic_b, session_key=session_key(client)
        )
        assert len(issued.codes) == 10
        assert RecoveryCode.objects.filter(batch_id=issued.batch_id).count() == 10
        with pytest.raises(LifecycleDeniedError):
            issue_recovery_codes(
                clinic_id=rbac_graph.clinic_b, session_key=session_key(client)
            )
        consumed = recover_authenticator(
            clinic_id=rbac_graph.clinic_b, code=issued.codes[0]
        )
        repeated = recover_authenticator(
            clinic_id=rbac_graph.clinic_b, code=issued.codes[0]
        )
    assert consumed
    assert not repeated
    with owner_context(rbac_graph.organization_a):
        assert (
            RecoveryCode.objects.filter(
                batch_id=issued.batch_id, used_at__isnull=False
            ).count()
            == 1
        )
        assert (
            SessionRecord.objects.get(
                session_key_hash=session_hash(session_key(client))
            ).revoked_at
            is not None
        )


def test_native_recovery_consumption_logs_out_and_refuses_the_old_cookie(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    client = verified_client(graph)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        issued = issue_recovery_codes(
            clinic_id=graph.clinic_b, session_key=session_key(client)
        )
    old_cookies = deepcopy(client.cookies)
    with runtime_role():
        response = client.post(
            f"/clinics/{graph.clinic_b}/account/recovery/",
            {"code": issued.codes[0]},
        )
    assert response.status_code == 302
    assert response.headers["Location"] == "/auth/login/"
    assert "_auth_user_id" not in client.session

    replay = Client()
    replay.cookies = old_cookies
    with runtime_role():
        refused = replay.get("/auth/protected/")
    assert refused.status_code == 302
    assert refused.headers["Location"].startswith("/auth/login/")


def _requested_reset(graph: RbacGraph, client: Client) -> AuthenticatorReset:
    create_totp_device(graph.clinic_admin, confirmed=True)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        return request_authenticator_reset(
            clinic_id=graph.clinic_b,
            user_id=graph.clinic_admin,
            session_key=session_key(client),
        )


def _deliver(reset: AuthenticatorReset) -> None:
    # Earlier outbox fixtures clear the process-wide adapter registry. Rebuild
    # the identity worker's actual startup registrations before its send.
    register_identity_notifications()
    operation_id = reset.notification_operation_id
    assert operation_id is not None
    with runtime_role():
        outcome = execute_operation(operation_id)
    assert outcome == "succeeded"
    body = json.dumps(
        {
            "event_id": f"synthetic-reset-{uuid4().hex}",
            "provider_reference": f"synthetic:identity-email:{operation_id}",
            "status": "delivered",
        }
    ).encode()
    key = salted_hmac(
        "identity.email.callback.key", EMAIL_PROVIDER, algorithm="sha256"
    ).digest()
    signature = hmac.new(key, body, hashlib.sha256).hexdigest()
    with runtime_role():
        outcome_callback = receive_provider_callback(
            provider=EMAIL_PROVIDER,
            headers={SIGNATURE_HEADER: signature},
            body=body,
        )
    assert outcome_callback == "applied"


def test_reset_cannot_execute_without_notification(
    rbac_graph: RbacGraph,
) -> None:
    client = verified_client(rbac_graph)
    reset = _requested_reset(rbac_graph, client)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(LifecycleDeniedError),
    ):
        execute_authenticator_reset(
            clinic_id=rbac_graph.clinic_b,
            reset_id=reset.pk,
            session_key=session_key(client),
        )


def test_delivered_notification_starts_full_database_delay(
    rbac_graph: RbacGraph, settings: SettingsWrapper
) -> None:
    settings.COMMS_SYNTHETIC_CHANNELS = ("email",)
    client = verified_client(rbac_graph)
    reset = _requested_reset(rbac_graph, client)
    destination = "synthetic-reset@example.invalid"
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_user SET email=%s WHERE id=%s",
            [destination, rbac_graph.clinic_admin],
        )
    _deliver(reset)
    with owner_context(rbac_graph.organization_a):
        reset.refresh_from_db()
    assert reset.notified_at is not None
    assert reset.available_at == reset.notified_at + timedelta(hours=24)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(LifecycleDeniedError),
    ):
        execute_authenticator_reset(
            clinic_id=rbac_graph.clinic_b,
            reset_id=reset.pk,
            session_key=session_key(client),
        )
