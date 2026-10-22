"""Exercise the small startup registry contracts without granting authority."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from operator import methodcaller
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity import offboarding_registry, onboarding_registry

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.offboarding_registry import OffboardingHandler
    from apps.identity.onboarding_registry import CompletionCheck


async def _async_complete(*, clinic_id: UUID) -> bool:
    return clinic_id.int != 0


async def _async_transfer(*, clinic_id: UUID, user_id: UUID) -> None:
    if clinic_id == user_id:
        message = "Invalid synthetic pair"
        raise ValueError(message)


@pytest.fixture(autouse=True)
def isolated_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding_registry, "_steps", {})
    monkeypatch.setattr(offboarding_registry, "_handlers", [])


def test_step_registration_preserves_callback_and_snapshot() -> None:
    selected = uuid4()

    def complete(*, clinic_id: UUID) -> bool:
        return clinic_id == selected

    onboarding_registry.register_onboarding_step(
        "services_resources", "clinic.manage", complete
    )
    snapshot = onboarding_registry.registered_onboarding_steps()
    assert len(snapshot) == 1
    assert snapshot[0].completion_check(clinic_id=selected)
    assert not snapshot[0].completion_check(clinic_id=uuid4())
    with pytest.raises(FrozenInstanceError):
        methodcaller("__setattr__", "key", "replaced")(snapshot[0])
    onboarding_registry.register_onboarding_step("recovery_enrollment", None, complete)
    assert len(snapshot) == 1
    assert len(onboarding_registry.registered_onboarding_steps()) == 2


def test_duplicate_step_cannot_replace_completion_check() -> None:
    selected = uuid4()

    def complete(*, clinic_id: UUID) -> bool:
        return clinic_id == selected

    def incomplete(*, clinic_id: UUID) -> bool:
        return clinic_id != selected

    onboarding_registry.register_onboarding_step(
        "opening_agenda", "scheduling.write", complete
    )
    with pytest.raises(onboarding_registry.DuplicateOnboardingStepError):
        onboarding_registry.register_onboarding_step(
            "opening_agenda", "clinic.manage", incomplete
        )
    step = onboarding_registry.registered_onboarding_steps()[0]
    assert step.completion_check(clinic_id=selected)
    assert step.permission == "scheduling.write"


@pytest.mark.parametrize("completion_check", [_async_complete])
def test_async_completion_cannot_escape_the_transaction(
    completion_check: CompletionCheck,
) -> None:
    with pytest.raises(TypeError):
        onboarding_registry.register_onboarding_step(
            "opening_agenda", "scheduling.write", completion_check
        )
    assert not onboarding_registry.registered_onboarding_steps()


@pytest.mark.parametrize("handler", [_async_transfer])
def test_async_handler_cannot_escape_the_transaction(
    handler: OffboardingHandler,
) -> None:
    with pytest.raises(TypeError):
        offboarding_registry.register_offboarding_handler(handler)
    assert not offboarding_registry.registered_offboarding_handlers()


def test_offboarding_registry_is_empty_and_preserves_handler_scope() -> None:
    assert offboarding_registry.registered_offboarding_handlers() == ()
    observed: list[tuple[UUID, UUID]] = []

    def transfer(*, clinic_id: UUID, user_id: UUID) -> None:
        observed.append((clinic_id, user_id))

    offboarding_registry.register_offboarding_handler(transfer)
    snapshot = offboarding_registry.registered_offboarding_handlers()
    assert not observed
    clinic_id, user_id = uuid4(), uuid4()
    snapshot[0](clinic_id=clinic_id, user_id=user_id)
    assert observed == [(clinic_id, user_id)]
    with pytest.raises(offboarding_registry.DuplicateOffboardingHandlerError):
        offboarding_registry.register_offboarding_handler(transfer)
    assert offboarding_registry.registered_offboarding_handlers() == snapshot
