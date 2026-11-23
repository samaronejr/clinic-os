"""The chart must not silently drop its latest clinician-accepted AI use."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from apps.scribe.models import ReviewDecision
from apps.scribe.templatetags import scribe_review


def test_chart_history_includes_the_latest_distinct_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        SimpleNamespace(
            pk=n,
            created_at=n,
            section="plan",
            action="accepted",
            capability_snapshot={"configuration_id": str(UUID(int=n)), "version": n},
            result_revision=n,
        )
        for n in range(1, 102)
    ]
    query = MagicMock()
    query.defer.return_value = query
    query.order_by.side_effect = lambda first, *rest: sorted(
        records, key=lambda record: record.created_at, reverse=first.startswith("-")
    )
    monkeypatch.setattr(ReviewDecision.objects, "filter", lambda **kwargs: query)
    result = scribe_review.note_ai_history(UUID(int=42), UUID(int=43))
    rows = result["ai_use_records"]
    assert isinstance(rows, list)
    assert rows[0]["configuration_version"] == rows[0]["revision"] == 101
    assert len(rows) == 100
    older = scribe_review.note_ai_history(UUID(int=42), UUID(int=43), "2")[
        "ai_use_records"
    ]
    assert isinstance(older, list)
    assert [item["revision"] for item in [*rows, *older]] == list(range(101, 0, -1))
