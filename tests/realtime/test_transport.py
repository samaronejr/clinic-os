"""PHI-free transport and commit-only publication contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.realtime.stream import _event
from apps.realtime.transport import event_bytes, topic_hash

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper


def test_event_schema_is_closed_and_topic_is_opaque(settings: SettingsWrapper) -> None:
    settings.REALTIME_TOPIC_SECRET = uuid4().hex
    topic = f"clinic:{uuid4()}:agenda"
    payload = json.loads(event_bytes(topic, "agenda", 1))
    assert payload == {"topic_hash": topic_hash(topic), "kind": "agenda", "version": 1}
    assert len(payload["topic_hash"]) == 64
    assert topic not in json.dumps(payload)


@pytest.mark.parametrize(
    ("kind", "version"),
    [
        ("SINTETICO-SENTINELA-PHI", 1),
        ("agenda\ndata: forged", 1),
        ("agenda", True),
        ("agenda", -1),
    ],
)
def test_unregistered_kind_or_invalid_version_cannot_carry_text(
    kind: str, version: int
) -> None:
    with pytest.raises(ValueError, match="invalid realtime event"):
        event_bytes(f"clinic:{uuid4()}:agenda", kind, version)


@pytest.mark.parametrize(
    "change",
    [
        {"text": "SINTETICO-SENTINELA-PHI"},
        {"kind": "SINTETICO-SENTINELA-PHI"},
        {"kind": []},
        {"topic_hash": "SINTETICO-SENTINELA-PHI"},
        {"topic_hash": []},
        {"version": True},
        {"version": -1},
        {"version": 2**53},
    ],
)
def test_broker_boundary_rejects_extra_fields_and_forged_metadata(
    change: dict[str, object],
) -> None:
    topic = f"clinic:{uuid4()}:agenda"
    value = json.loads(event_bytes(topic, "agenda", 1))
    value.update(change)
    assert _event(json.dumps(value).encode(), frozenset({topic_hash(topic)})) is None
