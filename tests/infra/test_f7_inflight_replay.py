"""An already executed attempt must never reach the inflight waiting path."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from apps.comms.models import IntegrationOperation
from ops.testing import migration_upgrade_inbox_verify as verifier


def test_succeeded_attempt_is_refused_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = SimpleNamespace(status="succeeded")
    lookup = Mock(return_value=operation)
    event = Mock(side_effect=AssertionError("must not create a rendezvous"))
    monkeypatch.setattr(IntegrationOperation.objects, "get", lookup)
    monkeypatch.setattr(verifier, "Event", event)
    world = Mock()
    world.owner.return_value = nullcontext()
    errors: list[str] = []
    verifier._r7_inflight(
        world, {"message": str(UUID(int=1)), "a": str(UUID(int=2))}, errors
    )

    assert errors == ["r7_inflight_precondition: expected pending attempt"]
    lookup.assert_called_once_with(pk=UUID(int=2))
    world.dispatch.assert_not_called()
    event.assert_not_called()
