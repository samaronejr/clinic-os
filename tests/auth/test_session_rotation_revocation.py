"""REPRODUCER (expected to fail until fixed): rotation revives a revoked session.

``TenantMiddleware`` admits a staff request with one plain read of
``identity_session_active`` (apps/tenancy/middleware.py:175) and takes no lock.
A revocation can commit after that read and before the request's OTP step
rotates the session key. The rotation then supersedes nothing, because
``identity_session_rotate_hash`` only updates rows ``WHERE revoked_at IS NULL``
(apps/identity/migrations/_session_rotation_sql.py). ``supersede_session_hash``
drops that result (apps/identity/session_inventory.py:145), and
``record_verified_session`` registers the new key unconditionally (:191-200).
``identity_session_register`` checks only the user, never the old session
(apps/identity/migrations/_lifecycle_sessions_sql.py:6-48).

The state after that interleaving is exactly "the session is revoked, then the
rotation runs for its old key", so this drives the real rotation service in
that order. No threads or timing are involved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import SessionRecord, User
from apps.identity.otp import record_otp_verification
from apps.identity.session_inventory import (
    SessionInventoryDeniedError,
    revoke_session,
    session_hash,
    session_is_active,
)
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory

from otp_test_support import create_totp_device, runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

COOKIE = settings.SESSION_COOKIE_NAME


def test_rotating_a_revoked_session_leaves_no_live_session(
    rbac_graph: RbacGraph,
) -> None:
    # Given a signed-in staff session whose inventory row is then revoked.
    user = User.objects.get(pk=rbac_graph.physician)
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    browser = Client()
    with runtime_role():
        assert browser.login(username=user.username, password=RBAC_RAW_CREDENTIAL)
        old_key = browser.cookies[COOKIE].value
        with tenant_context(rbac_graph.physician, rbac_graph.organization_a):
            record = SessionRecord.objects.get(session_key_hash=session_hash(old_key))
            revoke_session(session_record_id=record.pk)
        assert not session_is_active(user_id=user.pk, session_key=old_key)

        # When the request already admitted on that session rotates its key.
        request = RequestFactory().post("/auth/verify/")
        request.session = SessionStore(session_key=old_key)
        request.user = user
        try:
            record_otp_verification(request, device)
        except SessionInventoryDeniedError:
            refused = True
        else:
            refused = False
        new_key = request.session.session_key

        # Then the rotation is refused, or at least leaves no live session.
        assert new_key is not None
        assert new_key != old_key
        revived = session_is_active(user_id=user.pk, session_key=new_key)
        assert not revived, (
            f"rotation of a revoked session registered a live session "
            f"(refused={refused})"
        )
