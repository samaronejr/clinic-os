"""Shared helpers for provider capability gate tests.

Transactional tests flush migration seeds, so every test that needs the
registry rebuilds it through ``seed_capabilities`` — the same function the
``providers.0001`` migration runs. ``activate_capability`` drives the real
owner lifecycle (propose -> approve -> activate) so the trigger, approval
binding and audit events are exercised, never bypassed; call it outside
``runtime_role()`` because it asserts the ``clinic_owner`` role.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import django.apps
from apps.providers import lifecycle
from apps.providers.migrations._seed_v2 import seed_v2_capabilities
from apps.providers.models import CapabilityVersion, ProviderCapability
from apps.providers.services import current_version, is_live

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

APPROVER = lifecycle.ApprovalInput(
    approver_name="Synthetic Owner",
    approver_role="clinic owner",
    evidence_uri="synthetic://evidence/provider-approval",
)
APPROVER_ARGS: dict[str, str] = {
    "approver_name": APPROVER.approver_name,
    "approver_role": APPROVER.approver_role,
    "evidence_uri": APPROVER.evidence_uri,
}


def seed_capabilities() -> None:
    """Rebuild the 2026-09-24-v2 registry seed in the current database."""
    seed_v2_capabilities(django.apps.apps)


def activate_capability(key: str) -> CapabilityVersion:
    """Drive ``key`` to ``activated`` through the real owner lifecycle.

    The seeded current version is ``selected_in_plan`` for PV-covered
    capabilities and ``researched`` otherwise; a researched version is
    first moved to ``selected_in_plan`` so the legal walk applies to both.
    """
    capability = ProviderCapability.objects.get(key=key, clinic_id__isnull=True)
    version = capability.current_version
    assert version is not None
    if version.state == CapabilityVersion.State.RESEARCHED:
        version.state = CapabilityVersion.State.SELECTED_IN_PLAN
        version.save(update_fields=["state", "updated_at"])
    lifecycle.approve_version(key, decision=APPROVER)
    return lifecycle.activate_version(key, decision=APPROVER)


def assert_capability_gate_closed(settings: SettingsWrapper, key: str) -> None:
    """Assert the three-condition gate stays closed under the real suite.

    Proves, without stubbing the gate: a ``researched`` current version is
    not live; a version forced to ``activated`` through the owner path is
    still not live while ``CLINIC_DATA_MODE`` is ``synthetic``; and the
    same activated version is not live when the mode setting is absent or
    invalid. The process data mode is never flipped to live.
    """
    seed_capabilities()
    lifecycle.propose_version(
        key,
        version_input=lifecycle.VersionInput(provider="synthetic-candidate"),
    )
    researched = current_version(key)
    assert researched is not None
    assert researched.state == CapabilityVersion.State.RESEARCHED
    assert is_live(key, clinic_id=None) is False

    activate_capability(key)
    activated = current_version(key)
    assert activated is not None
    assert activated.state == CapabilityVersion.State.ACTIVATED
    assert settings.CLINIC_DATA_MODE == "synthetic"
    assert is_live(key, clinic_id=None) is False

    del settings.CLINIC_DATA_MODE
    assert is_live(key, clinic_id=None) is False
    settings.CLINIC_DATA_MODE = "not-a-mode"
    assert is_live(key, clinic_id=None) is False
