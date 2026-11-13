"""PY-D1: a payment-provider outage shows a clear status and changes nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.billing.models import Invoice, PixCharge, PixOperation, Receipt
from apps.billing.views import PIX_UNAVAILABLE_DETAIL

from renewal.test_billing_views import (
    FOUND,
    Scenario,
    _create_fields,
    _get,
    _post,
    staff_client,
)

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
UNAVAILABLE = 503


def test_payment_provider_outage_shows_a_clear_status_and_creates_nothing(
    rbac_graph: RbacGraph, settings: SettingsWrapper
) -> None:
    # Given an issued charge while no payment provider is available.
    settings.BILLING_SYNTHETIC_PIX = False
    scenario = Scenario(rbac_graph)
    with staff_client(scenario.manager.pk) as client:
        assert (
            _post(client, scenario.charges_url, _create_fields(client, scenario))
            == FOUND
        )
        with scenario.scope():
            invoice = Invoice.objects.get()
        url = scenario.invoice_url(invoice.pk)
        assert (
            _post(client, url, {"action": "issue", "expected_revision": "1"}) == FOUND
        )
        # When reception asks for a payment code.
        response = client.post(
            url,
            {
                "action": "code",
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            },
        )
        # Then the page says exactly why, nothing was created and no payment claimed.
        assert response.status_code == UNAVAILABLE
        assert PIX_UNAVAILABLE_DETAIL in response.content.decode()
        with scenario.scope():
            assert not PixOperation.objects.exists()
            assert not PixCharge.objects.exists()
            assert not Receipt.objects.exists()
        _status, page = _get(client, url)
        assert 'data-payment-state="issued"' in page
