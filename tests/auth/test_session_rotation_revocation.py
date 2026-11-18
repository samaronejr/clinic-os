"""A rotation never revives a revoked session, nor rotates another user's.

``TenantMiddleware`` admits a staff request with one plain read of
``identity_session_active`` (apps/tenancy/middleware.py:175) and takes no lock,
so a revocation can commit after that read and before the request's OTP step
rotates the session key. The old two-step rotation then superseded nothing,
ignored that, and registered the new key as live.
``identity_session_rotate`` (identity 0054) supersedes the old key only while
it is live and the current user's, under the per-user lock the revocation
trigger takes, and registers the new key only then.

The state after that interleaving is exactly "the session is revoked, then the
rotation runs for its old key", so these tests drive the real rotation service
in that order, as the verify view does: a genuine code checked at the resolver,
then ``record_otp_verification``, in one transaction. No threads or timing are
involved. A control rotates a live session the same way and must succeed, so
a setup defect cannot pass for a refusal.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import SessionRecord, User
from apps.identity.otp import record_otp_verification, verify_totp_code
from apps.identity.session_inventory import (
    SessionInventoryDeniedError,
    revoke_session,
    rotate_session,
    session_hash,
    session_is_active,
)
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.test import Client, RequestFactory

from otp_test_support import (
    create_totp_device,
    current_user_guc,
    fixed_otp_time,
    runtime_role,
    token_for,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

COOKIE = settings.SESSION_COOKIE_NAME


def _verify_and_rotate(user: User, device: TOTPDevice, old_key: str) -> str:
    """Check a genuine code, then rotate, in one transaction as the view does.

    Returns the rotated key; a refusal rolls the whole transaction back and
    propagates ``SessionInventoryDeniedError``.
    """
    request = RequestFactory().post("/auth/verify/")
    request.session = SessionStore(session_key=old_key)
    request.user = user
    with fixed_otp_time(), transaction.atomic(), current_user_guc(user.pk):
        assert verify_totp_code(device.pk, token_for(device)) == 1
        record_otp_verification(request, device)
    key = request.session.session_key
    assert key is not None
    assert key != old_key
    return key


def _signed_in(user: User) -> str:
    browser = Client()
    assert browser.login(username=user.username, password=RBAC_RAW_CREDENTIAL)
    return browser.cookies[COOKIE].value


def test_rotating_a_live_session_registers_the_new_key(rbac_graph: RbacGraph) -> None:
    # Control: the same verify-then-rotate on a live session succeeds.
    user = User.objects.get(pk=rbac_graph.physician)
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    with runtime_role():
        old_key = _signed_in(user)
        new_key = _verify_and_rotate(user, device, old_key)
        assert session_is_active(user_id=user.pk, session_key=new_key)
        assert not session_is_active(user_id=user.pk, session_key=old_key)


def test_rotating_a_revoked_session_leaves_no_live_session(
    rbac_graph: RbacGraph,
) -> None:
    # Given a signed-in staff session whose inventory row is then revoked.
    user = User.objects.get(pk=rbac_graph.physician)
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    with runtime_role():
        old_key = _signed_in(user)
        with tenant_context(rbac_graph.physician, rbac_graph.organization_a):
            record = SessionRecord.objects.get(session_key_hash=session_hash(old_key))
            revoke_session(session_record_id=record.pk)
        assert not session_is_active(user_id=user.pk, session_key=old_key)

        # When the request already admitted on that session verifies its code
        # and rotates its key.
        refused = False
        try:
            new_key = _verify_and_rotate(user, device, old_key)
        except SessionInventoryDeniedError:
            refused = True

        # Then the rotation is refused and registers no live session.
        live = not refused and session_is_active(user_id=user.pk, session_key=new_key)
        assert refused, (
            "rotation of a revoked session registered a live session "
            f"(new key live: {live})"
        )


def test_a_rotation_for_another_users_session_is_refused(
    rbac_graph: RbacGraph,
) -> None:
    # Given the physician's live session.
    physician = User.objects.get(pk=rbac_graph.physician)
    with runtime_role():
        old_key = _signed_in(physician)
        new_key = secrets.token_urlsafe(32)

        # When another staff user's request tries to rotate it.
        with pytest.raises(SessionInventoryDeniedError):
            rotate_session(
                user_id=rbac_graph.clinic_admin,
                old_key=old_key,
                new_key=new_key,
                expires_at=SessionStore(session_key=old_key).get_expiry_date(),
                meta={"REMOTE_ADDR": "127.0.0.1", "HTTP_USER_AGENT": ""},
            )

        # Then nothing changed: the physician's key stays live, no new key.
        assert session_is_active(user_id=physician.pk, session_key=old_key)
        for owner in (physician.pk, rbac_graph.clinic_admin):
            assert not session_is_active(user_id=owner, session_key=new_key)
