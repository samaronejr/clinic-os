"""Exact event fencing uses real PostgreSQL sessions, not scheduler delays."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from apps.orders.lab import receive_lab_result
from apps.orders.models import ResultVersion, ResultWorkItem
from django.db import close_old_connections, connections

from identity.permission_support import owner_context
from orders.support import laboratory
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.orders.values import IngestReceipt
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_signed_replay_commits_one_version_and_one_review_task(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    body = lab.body()
    headers = lab.headers(body)
    start = Barrier(2)

    def receive() -> IngestReceipt:
        close_old_connections()
        try:
            with runtime_role():
                start.wait(timeout=15)
                return receive_lab_result(headers=headers, body=body)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(receive), pool.submit(receive)
        receipts = first.result(timeout=30), second.result(timeout=30)
    assert receipts[0] == receipts[1]
    with owner_context(rbac_graph.organization_a):
        assert ResultVersion.objects.count() == 1
        assert ResultWorkItem.objects.count() == 1
