"""Step-up denials retain their identity through ordinary context managers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from apps.identity.stepup import StepUpFailureReason, StepUpRequired

if TYPE_CHECKING:
    from collections.abc import Iterator


def test_step_up_required_survives_generator_context_manager() -> None:
    @contextmanager
    def boundary() -> Iterator[None]:
        yield

    denied = StepUpRequired()
    with pytest.raises(StepUpRequired) as raised, boundary():
        raise denied
    assert raised.value is denied
    assert raised.value.reason is StepUpFailureReason.RECENT_VERIFICATION_REQUIRED
