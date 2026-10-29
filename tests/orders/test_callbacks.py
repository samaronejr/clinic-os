"""Forged, replayed, revoked and out-of-order callbacks on real tenant roles."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import ServicePrincipal
from apps.orders.lab import LabCallbackRejectedError, lab_context, receive_lab_result
from apps.orders.models import ResultVersion, ResultWorkItem
from apps.orders.services import (
    OrdersAccessDeniedError,
    OrdersConflictError,
    acknowledge_result,
)
from django.db import connection
from django.test.utils import CaptureQueriesContext

from identity.permission_support import owner_context
from orders.support import laboratory
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_signed_replay_creates_one_source_version_and_review_task(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    body = lab.body()
    first = lab.receive(body)
    replay = lab.receive(body)
    assert (first.version_id, first.number, first.state) == (
        replay.version_id,
        1,
        "matched",
    )
    with owner_context(rbac_graph.organization_a):
        assert ResultVersion.objects.count() == 1
        assert ResultWorkItem.objects.count() == 1


def test_forged_hmac_refuses_before_any_scope_query(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    body = lab.body()
    with (
        runtime_role(),
        CaptureQueriesContext(connection) as queries,
        pytest.raises(LabCallbackRejectedError),
    ):
        receive_lab_result(
            headers={
                "X-Lab-Account": str(lab.account),
                "X-Lab-Signature": "0" * 64,
            },
            body=body,
        )
    assert not queries.captured_queries


def test_signed_payload_tenant_claim_is_refused(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    value = json.loads(lab.body())
    value["clinic_id"] = str(rbac_graph.clinic_c)
    body = json.dumps(value).encode()
    with pytest.raises(LabCallbackRejectedError):
        lab.receive(body)


def test_out_of_order_correction_cannot_create_a_series(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    value = json.loads(lab.body())
    value.update(result_id=str(uuid4()), predecessor_id=str(uuid4()))
    with pytest.raises(OrdersConflictError, match="source_superseded"):
        lab.receive(json.dumps(value).encode())
    with owner_context(rbac_graph.organization_a):
        assert not ResultVersion.objects.exists()


def test_revoked_principal_cannot_ingest_even_previously_signed_event(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    body = lab.body()
    with owner_context(rbac_graph.organization_a):
        ServicePrincipal.objects.filter(pk=lab.principal).update(active=False)
    with pytest.raises(LabCallbackRejectedError):
        lab.receive(body)


def test_lab_principal_cannot_select_sources_or_acknowledge(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    receipt = lab.receive(lab.body())
    with runtime_role(), lab_context(account_id=lab.account, proof=lab.key):
        assert not ResultVersion.objects.exists()
        with pytest.raises(OrdersAccessDeniedError):
            acknowledge_result(
                clinic_id=rbac_graph.clinic_a,
                version_id=receipt.version_id,
                disposition="no_action",
            )


@pytest.mark.parametrize(("mode", "enabled"), [("synthetic", False), ("live", True)])
def test_disabled_or_live_lab_gate_refuses_before_any_database_query(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    mode: str,
    enabled: bool,
) -> None:
    lab = laboratory(rbac_graph, settings)
    body = lab.body()
    settings.CLINIC_DATA_MODE = mode
    settings.ORDERS_SYNTHETIC_LAB_ENABLED = enabled
    with (
        runtime_role(),
        CaptureQueriesContext(connection) as queries,
        pytest.raises(LabCallbackRejectedError),
    ):
        receive_lab_result(headers=lab.headers(body), body=body)
    assert not queries.captured_queries
