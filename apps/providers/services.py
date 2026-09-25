"""Runtime read surface of the provider capability registry.

``is_live`` is the single gate every provider-backed code path must ask
before treating a capability as real. It returns True only when ALL THREE
conditions hold:

(i)   the capability's current ``CapabilityVersion`` is in state
      ``activated``;
(ii)  the resolved process data mode equals ``LIVE_DATA_MODE`` — read from
      ``django.conf.settings.CLINIC_DATA_MODE`` (validated at startup by
      ``require_data_mode``) or from an explicitly injected ``environment``
      mapping in unit tests;
(iii) ``ops.release.activation.require_live_runtime`` does not raise for
      the injected environment or ``os.environ``.

Condition (ii) is mandatory and is NOT implied by (iii):
``require_live_runtime`` is a documented no-op outside live mode, so an
``activated`` registry row alone must never report live under the
synthetic suite. The mode check runs first so the gate never touches the
database outside live mode.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import UUID

from config.settings.contracts import LIVE_DATA_MODE
from django.conf import settings
from ops.release.activation import LiveModeHaltedError, require_live_runtime

from apps.providers.models import CapabilityVersion, ProviderCapability

__all__ = ("current_version", "is_live")


def _capability_for(key: str, clinic_id: UUID | None) -> ProviderCapability | None:
    """Return the clinic override when present, else the platform row."""
    capabilities = ProviderCapability.objects.select_related("current_version")
    if clinic_id is not None:
        scoped = capabilities.filter(key=key, clinic_id=clinic_id).first()
        if scoped is not None:
            return scoped
    return capabilities.filter(key=key, clinic_id__isnull=True).first()


def current_version(
    key: str, *, clinic_id: UUID | None = None
) -> CapabilityVersion | None:
    """Return the capability's current version, or None when unregistered."""
    capability = _capability_for(key, clinic_id)
    if capability is None:
        return None
    return capability.current_version


def is_live(
    key: str,
    *,
    clinic_id: UUID | None,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return True only when the capability may serve real traffic now.

    Fails closed on unknown keys, unknown scopes, non-activated states,
    non-live or absent data modes and halted live runtimes. ``clinic_id``
    selects a per-clinic override row when one exists and falls back to
    the platform row; ``None`` evaluates the platform row directly.
    """
    if environment is not None and not isinstance(environment, Mapping):
        return False
    runtime_environment = os.environ if environment is None else environment
    if environment is not None:
        mode = environment.get("CLINIC_DATA_MODE")
    else:
        mode = getattr(settings, "CLINIC_DATA_MODE", None)
    if mode != LIVE_DATA_MODE:
        return False
    if clinic_id is not None and type(clinic_id) is not UUID:
        return False
    version = current_version(key, clinic_id=clinic_id)
    if version is None or version.state != CapabilityVersion.State.ACTIVATED:
        return False
    try:
        require_live_runtime(runtime_environment)
    except LiveModeHaltedError:
        return False
    return True
