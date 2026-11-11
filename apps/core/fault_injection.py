"""Settings-driven faults for the synthetic provider adapters (todo 72).

Degraded-mode drills set ``CLINIC_SYNTHETIC_FAULTS`` (parsed at startup by
``config.settings.contracts.resolve_synthetic_faults``) to make one synthetic
provider family unavailable (``down``), past its deadline (``slow``) or unsure
of its outcome (``ambiguous``). Each synthetic adapter asks ``injected_fault``
on its already-enabled synthetic path and raises its own existing failure
vocabulary, so the product's real failure handling runs unchanged. Nothing
here reaches a provider, and live data mode refuses any configured fault.
"""

from __future__ import annotations

from typing import Final, Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

type FaultFamily = Literal["ai", "video", "messaging", "signing", "payment"]
type FaultMode = Literal["down", "slow", "ambiguous"]

_MODES: Final[dict[str, FaultMode]] = {
    "down": "down",
    "slow": "slow",
    "ambiguous": "ambiguous",
}


def injected_fault(family: FaultFamily) -> FaultMode | None:
    """Return the drill fault configured for one synthetic provider family.

    A configured fault outside synthetic data mode, or an unknown mode, fails
    closed instead of being ignored.
    """
    faults: object = getattr(settings, "CLINIC_SYNTHETIC_FAULTS", None)
    if not faults:
        return None
    if settings.CLINIC_DATA_MODE != "synthetic" or not isinstance(faults, dict):
        message = "synthetic provider faults require synthetic data mode"
        raise ImproperlyConfigured(message)
    configured: object = faults.get(family)
    if configured is None:
        return None
    mode = _MODES.get(configured) if isinstance(configured, str) else None
    if mode is None:
        message = "synthetic provider fault mode is not in the closed vocabulary"
        raise ImproperlyConfigured(message)
    return mode
