"""Executed signed-wire passkey authority across every stored staff role."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity import stepup
from apps.identity.models import Organization, Passkey, User, UserClinicRole
from apps.identity.passkey_challenges import PasskeyDeniedError
from apps.identity.passkey_controls import require_authenticator_session
from apps.identity.passkey_sessions import is_passkey_verified
from apps.prescription import views as prescription_views
from apps.tenancy.db import tenant_context
from django.test import Client, RequestFactory, override_settings

from auth.passkey_test_support import (
    AssertionOptions,
    passkey_login,
    registered_passkey,
)
from auth.stepup_test_support import create_role_actor
from identity.lifecycle_test_support import session_key
from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def _prescription_freshness(user: User, client: Client) -> dict[str, object]:
    """Read real signing readiness from the signed session under a controlled clock."""
    request = RequestFactory().get("/auth/step-up/")
    request.user = user
    request.session = client.session
    verified_at = request.session[stepup.STEP_UP_SESSION_KEY]
    assert type(verified_at) is int
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(stepup, "_utc_now_seconds", lambda: verified_at)
        return prescription_views._identity_state(request)


def exercise_passkey_authority(graph: RbacGraph, role: str) -> None:
    """Observe real acceptance/refusal, never a stubbed verifier or harvested tuple."""
    actor = create_role_actor(graph, UserClinicRole.Role(role))
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            user_id=actor.pk,
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            role=role,
        )
    subject = replace(graph, clinic_admin=actor.pk)
    registered = registered_passkey(subject)
    signed = passkey_login(registered)
    with runtime_role():
        own_page = registered.client.get("/account/passkeys/")
        policy = registered.client.post(
            "/account/passkeys/policy/",
            {"clinic_id": str(graph.clinic_b), "enabled": "on"},
        )
    assert own_page.status_code == 200
    assert policy.status_code == (302 if role in {"owner", "org_admin"} else 403)
    with owner_context(graph.organization_a):
        Organization.objects.filter(pk=graph.organization_a).update(
            passkey_step_up_enabled=False
        )
    with runtime_role(), tenant_context(actor.pk, graph.organization_a):
        credential = Passkey.objects.get(user_id=actor.pk)
        assert credential.sign_count == 1
        assert not is_passkey_verified(session=signed.session, max_age=300)
        assert _prescription_freshness(actor, signed) == {
            "fresh": False,
            "valid_until": None,
        }
        with pytest.raises(PasskeyDeniedError):
            require_authenticator_session(session_key=session_key(signed))
    with owner_context(graph.organization_a):
        Organization.objects.filter(pk=graph.organization_a).update(
            passkey_step_up_enabled=True
        )
    with runtime_role(), tenant_context(actor.pk, graph.organization_a):
        assert is_passkey_verified(session=signed.session, max_age=300)
        require_authenticator_session(session_key=session_key(signed))
        readiness = _prescription_freshness(actor, signed)
        assert readiness["fresh"] is True
        assert isinstance(readiness["valid_until"], datetime)
        assert readiness["valid_until"].tzinfo is UTC
    # A signed foreign handle is not allowed to choose this account.
    foreign = Client()
    with runtime_role():
        options = foreign.post(
            "/auth/passkey/assert/begin/", {"purpose": "login"}
        ).json()
        refused = foreign.post(
            "/auth/passkey/assert/finish/",
            {
                "credential": registered.authenticator.assertion(
                    challenge=options["challenge"],
                    user_handle=registered.user_handle,
                    options=AssertionOptions(rp_id="foreign.example", sign_count=2),
                )
            },
        )
        unknown = registered.client.post(
            "/account/passkeys/remove/",
            {"clinic_id": str(graph.clinic_b), "passkey_id": str(uuid4())},
        )
        removed = registered.client.post(
            "/account/passkeys/remove/",
            {"clinic_id": str(graph.clinic_b), "passkey_id": str(credential.pk)},
        )
        stale = signed.get("/auth/protected/")
    assert refused.status_code == 403
    assert "_auth_user_id" not in foreign.session
    assert unknown.status_code == 403
    assert removed.status_code == 302
    assert stale.status_code == 302
    assert stale.headers["Location"].startswith("/auth/login/")
    with owner_context(graph.organization_a):
        assert Passkey.objects.get(pk=credential.pk).removed_at is not None
        Organization.objects.filter(pk=graph.organization_a).update(
            passkey_step_up_enabled=False
        )


def run_passkey_census(
    proofs: ExecutedProofs,
    graph: Callable[..., RbacGraph],
) -> None:
    """Attach named executable receipts to every actual entered passkey boundary."""
    with override_settings(
        WEBAUTHN_RP_ID="testserver", WEBAUTHN_ORIGIN="http://testserver"
    ):
        for role in UserClinicRole.Role.values:
            subject = graph()
            proofs.run(
                f"passkey signed-wire own scope and retirement:{role}",
                partial(
                    test_actual_passkey_authority_for_every_staff_role, subject, role
                ),
            )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("role", UserClinicRole.Role.values)
@override_settings(WEBAUTHN_RP_ID="testserver", WEBAUTHN_ORIGIN="http://testserver")
def test_actual_passkey_authority_for_every_staff_role(
    rbac_graph: RbacGraph, role: str
) -> None:
    """Independent oracle entrypoint is also runnable without the broad census."""
    exercise_passkey_authority(rbac_graph, role)


@pytest.mark.django_db(transaction=True)
def test_every_discovered_passkey_guard_has_an_executed_receipt(
    rbac_graph: RbacGraph,
) -> None:
    """A lost binding is a failure even when another boundary denies the request."""
    from identity.cut1_authority_census import ExecutedProofs  # noqa: PLC0415
    from identity.legacy_guard_inventory import discover  # noqa: PLC0415

    symbols = {
        name for name in discover() if name.startswith("apps.identity.passkey")
    } | {"apps.prescription.views._identity_state"}
    proofs = ExecutedProofs(symbols)
    run_passkey_census(proofs, lambda: rbac_graph)
    assert proofs.executed() == {name + "#cut1" for name in symbols}
    assert proofs.certificates["apps.prescription.views._identity_state"] == {
        f"passkey signed-wire own scope and retirement:{role}"
        for role in UserClinicRole.Role.values
    }


@pytest.mark.django_db(transaction=True)
def test_prescription_readiness_census_rejects_a_missing_binding() -> None:
    """Removing the helper's oracle must fail the actual staff-coverage gate."""
    from identity.guard_classification import assert_staff_coverage  # noqa: PLC0415
    from identity.legacy_guard_inventory import ROOT  # noqa: PLC0415
    from identity.staff_state_analysis import (  # noqa: PLC0415
        add_sql_staff_analysis,
        python_staff_analysis,
    )
    from identity.test_permission_parity import INVENTORY  # noqa: PLC0415

    symbol = "apps.prescription.views._identity_state"
    row = next(row for row in INVENTORY["candidates"] if row["symbol"] == symbol)
    analysis = python_staff_analysis(ROOT)
    add_sql_staff_analysis(analysis)
    assert "SQL table identity_userclinicrole" in analysis.evidence(symbol)
    assert_staff_coverage([row], analysis, {symbol + "#cut1"})
    with pytest.raises(
        AssertionError,
        match=r"apps\.prescription\.views\._identity_state.*missing executable oracle",
    ):
        assert_staff_coverage([row], analysis, set())
