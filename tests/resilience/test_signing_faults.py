"""Todo 72: a faulted signing provider never issues and stays recoverable."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.prescription.models import SignatureOperation

from renewal.test_signatures import owner_scope
from renewal.test_signatures import setup as signing_setup

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from renewal.test_signatures import SigningSetup

__all__ = ("signing_setup",)
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("mode", ["down", "slow"])
def test_an_unreachable_signer_keeps_the_operation_prepared_for_retry(
    signing_setup: SigningSetup, settings: SettingsWrapper, mode: str
) -> None:
    setup = signing_setup
    settings.CLINIC_SYNTHETIC_FAULTS = {"signing": mode}
    # When: the physician signs while the provider is faulted.
    operation = setup.request_signature()
    # Then: nothing is issued, no provider reference is bound, retry is possible.
    assert (operation.state, operation.operation_id, operation.failure_reason) == (
        "prepared",
        "",
        "",
    )
    assert setup.dispatch(operation.pk) == "retry"
    # Once the provider answers, the same operation completes exactly once.
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    assert setup.dispatch(operation.pk) == "signing"
    headers, body = setup.callback(setup.refresh(operation))
    assert setup.receive(headers, body) == "applied"
    assert setup.refresh(operation).state == "rehearsal_complete"
    with owner_scope(setup.graph.organization_a):
        assert SignatureOperation.objects.count() == 1


def test_an_acceptance_without_a_usable_reference_fails_closed(
    signing_setup: SigningSetup, settings: SettingsWrapper
) -> None:
    setup = signing_setup
    settings.CLINIC_SYNTHETIC_FAULTS = {"signing": "ambiguous"}
    operation = setup.request_signature()
    assert (operation.state, operation.failure_reason, operation.dispatch_started) == (
        "prepared",
        "",
        True,
    )
    assert setup.dispatch(operation.pk) == "reconcile"
    assert operation.signed_bytes is None or bytes(operation.signed_bytes) == b""
    # The same request is create-or-lookup; a malformed reply grants no new key.
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    restarted = setup.request_signature()
    assert restarted.pk == operation.pk
    assert restarted.state == "signing"
    with owner_scope(setup.graph.organization_a):
        assert list(
            SignatureOperation.objects.order_by("created_at").values_list(
                "state", "failure_reason"
            )
        ) == [("signing", "")]
