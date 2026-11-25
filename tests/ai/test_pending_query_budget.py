"""Count only statements executed while preparing a real authorized response."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.db import connection

from ai.test_care_final_fence import page
from ai.test_pending_render_commit import proposal_page
from ai.test_revenue_cycle import finance_setup, world
from core.test_navigation import _post

if TYPE_CHECKING:
    from collections.abc import Callable

    from ai.revenue_support import RevenueWorld
    from ai.test_care_final_fence import Page

__all__ = ("finance_setup", "page", "world")
pytestmark = pytest.mark.django_db(transaction=True)


class Count:
    def __init__(self) -> None:
        self.queries = self.fences = self.batches = 0

    def __call__(
        self,
        execute: Callable[..., object],
        sql: object,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        self.queries += 1
        self.fences += int("pg_try_advisory_xact_lock_shared" in str(sql))
        self.batches += int('"pending_render_group"' in str(sql))
        return execute(sql, params, many, context)

    def record(self, prop: Callable[[str, int], None]) -> None:
        assert self.fences == self.batches == 1
        prop("queries", self.queries)
        prop("shared_fences", self.fences)
        prop("final_batches", self.batches)


@pytest.mark.parametrize("transport", ["native", "htmx"])
def test_care_positive_query_count(
    page: Page, transport: str, record_property: Callable[[str, int], None]
) -> None:
    count = Count()
    with connection.execute_wrapper(count):
        response = _post(page.client, page.url, page.form)
    assert response.status_code == 200
    assert all(str(key.pk).encode() in response.content for key in page.keys)
    count.record(record_property)


@pytest.mark.parametrize("transport", ["native", "htmx"])
def test_finance_positive_query_count(
    world: RevenueWorld, transport: str, record_property: Callable[[str, int], None]
) -> None:
    page = proposal_page(world, "finance", transport)
    count = Count()
    with connection.execute_wrapper(count):
        status, content = page.fetch()
    assert status == 200
    assert all(marker in content for marker in page.markers)
    count.record(record_property)
