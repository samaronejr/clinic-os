"""Rows other transactions reference are never locked FOR UPDATE (review S5).

A foreign key's check takes ``FOR KEY SHARE`` on the referenced row: at commit
for this repository's ``DEFERRABLE INITIALLY DEFERRED`` keys, at once for an
immediate one. Only ``FOR UPDATE`` conflicts with it. Identity 0059 makes TOTP
verification lock its device ``FOR NO KEY UPDATE`` (H1), and SCIM's
``update_resource`` locks its resource the same way (H2).

The style is ``test_lock_modes.py``: a held first lock, server-side waits until
PostgreSQL reports each path blocked (bounded by ``statement_timeout``; no
sleep), then release. Before the fix each interleaving deadlocked and
PostgreSQL aborted one of the real requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.identity.enterprise import approve_group_mapping
from apps.identity.models import User
from apps.identity.scim import create_resource, scim_context
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.test import Client, override_settings

from auth.test_directory_lock_order import _Directory, _directory_user
from auth.test_lock_modes import SESSION_KEY, _Background, _deprovision, _deprovisioned
from identity.permission_support import permission_context
from otp_test_support import create_totp_device, fixed_otp_time, runtime_role, token_for
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

# The sync's own lock on the connection row is FOR UPDATE; holding it FOR NO
# KEY UPDATE blocks the sync but not the foreign-key checks of rows that
# reference the connection (SCIM token use, new resources).
HOLD_CONNECTION = (
    "SELECT 1 FROM clinic_app.identity_ssoconnection"
    " WHERE id = (SELECT connection_id FROM clinic_app.identity_scimresource"
    " WHERE id = %s) FOR NO KEY UPDATE"
)


def _empty_group(graph: RbacGraph, directory: _Directory, bundle: str) -> UUID:
    """A second mapped group the directory user is not yet a member of."""
    with runtime_role(), scim_context(bearer=directory.token) as scope:
        group = create_resource(
            clinic_id=scope.clinic_id,
            kind="Groups",
            attributes={"displayName": f"Synthetic {bundle}", "members": []},
        )
    with permission_context(graph, graph.clinic_admin):
        approve_group_mapping(
            clinic_id=graph.clinic_a,
            group_id=group.pk,
            unit_id=graph.clinic_a,
            bundle=bundle,
            session_key=directory.session_key,
        )
    return group.pk


def _scim_patch(path: str, token: str, operations: list[Mapping[str, object]]) -> str:
    response = Client().patch(
        path,
        data={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": operations,
        },
        content_type="application/scim+json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    return f"status {response.status_code}"


def _immediate_constraints(
    execute: Callable[..., object],
    sql: str,
    params: object,
    many: bool,
    context: Mapping[str, object],
) -> object:
    """Emulate immediate foreign keys: check each one as its row is written."""
    if connection.in_atomic_block:
        execute("SET CONSTRAINTS ALL IMMEDIATE", None, False, context)  # noqa: FBT003
    return execute(sql, params, many, context)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_h1_totp_verify_and_scim_deprovisioning(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # The verify view locks the device in identity_totp_verify, then waits for
    # the session key to rotate. SCIM, holding that key, retires the device:
    # its retirement row references the device.
    directory = _directory_user(rbac_graph, bundle="physician")
    account = directory.account
    with psycopg.connect(superuser_database_url, autocommit=True) as setup:
        setup.execute(
            "UPDATE clinic_app.identity_user SET password = %s WHERE id = %s",
            [make_password(RBAC_RAW_CREDENTIAL), account],
        )
    device = create_totp_device(account, confirmed=True)
    username = User.objects.get(pk=account).username
    client = Client()
    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
    code = token_for(device)

    def verify() -> str:
        with fixed_otp_time():
            response = client.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": code},
            )
        return f"status {response.status_code}"

    with psycopg.connect(superuser_database_url) as sign_in:
        sign_in.execute(SESSION_KEY, [account])
        scim = _deprovision(directory.resource, directory.token)
        scim.start_and_await_blocked(superuser_database_url)
        verifier = _Background(verify)
        verifier.start_and_await_blocked(superuser_database_url)
        sign_in.commit()
    # SCIM commits first; the rotation then refuses the revoked session.
    assert (scim.outcome(), verifier.outcome()) == ("status 200", "status 403")
    assert _deprovisioned(rbac_graph, account)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_h2_group_add_and_user_deprovisioning(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # A adds the user to a mapped group (a membership row referencing the
    # user's resource, checked at commit) and syncs; B locks that resource in
    # update_resource, then waits for the connection row A's sync holds.
    directory = _directory_user(rbac_graph)
    group = _empty_group(rbac_graph, directory, "physician")

    def add_member() -> str:
        return _scim_patch(
            f"/scim/v2/Groups/{group}",
            directory.token,
            [
                {
                    "op": "add",
                    "path": "members",
                    "value": [{"value": directory.resource}],
                }
            ],
        )

    with psycopg.connect(superuser_database_url) as holder:
        holder.execute(HOLD_CONNECTION, [directory.resource])
        adding = _Background(add_member)
        adding.start_and_await_blocked(superuser_database_url)
        scim = _deprovision(directory.resource, directory.token)
        scim.start_and_await_blocked(superuser_database_url)
        holder.commit()
    assert (adding.outcome(), scim.outcome()) == ("status 200", "status 200")
    assert _deprovisioned(rbac_graph, directory.account)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_h2_role_binding_with_immediate_foreign_keys(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    # The variant that needs no deferred key: a mapping approval's sync inserts
    # a role binding referencing the user's resource while it holds the
    # connection row, and the check runs at once.
    directory = _directory_user(rbac_graph)
    with runtime_role(), scim_context(bearer=directory.token) as scope:
        group = create_resource(
            clinic_id=scope.clinic_id,
            kind="Groups",
            attributes={
                "displayName": "Synthetic physicians",
                "members": [{"value": directory.resource}],
            },
        )

    def approve() -> str:
        with (
            connection.execute_wrapper(_immediate_constraints),
            permission_context(rbac_graph, rbac_graph.clinic_admin),
        ):
            approve_group_mapping(
                clinic_id=rbac_graph.clinic_a,
                group_id=group.pk,
                unit_id=rbac_graph.clinic_a,
                bundle="physician",
                session_key=directory.session_key,
            )
        return "completed"

    with psycopg.connect(superuser_database_url) as holder:
        holder.execute(HOLD_CONNECTION, [directory.resource])
        approval = _Background(approve)
        approval.start_and_await_blocked(superuser_database_url)
        scim = _deprovision(directory.resource, directory.token)
        scim.start_and_await_blocked(superuser_database_url)
        holder.commit()
    assert (approval.outcome(), scim.outcome()) == ("completed", "status 200")
    assert _deprovisioned(rbac_graph, directory.account)
