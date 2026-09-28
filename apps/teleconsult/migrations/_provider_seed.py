"""The synthetic video capability version rooms bind to in synthetic mode.

Frozen with migration ``teleconsult.0004``. The row is a ``video`` capability
version whose provider is the synthetic room adapter's key; it stays at
``researched`` forever (it is never a provider candidate for approval) and
its ``environment`` is ``synthetic``, which the room binding trigger accepts
only together with that provider key. Transactional tests flush the registry;
this function re-applies ``seed_v2_capabilities`` first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from apps.providers.migrations._seed_v2 import seed_v2_capabilities

if TYPE_CHECKING:
    from django.apps.registry import Apps

SYNTHETIC_VIDEO_VERSION: Final[dict[str, str]] = {
    "provider": "teleconsult-synthetic-v1",
    "account": "",
    "environment": "synthetic",
    "api_version": "v1",
    "region": "",
}


def seed_synthetic_video_version(apps: Apps) -> None:
    """Insert the synthetic video version idempotently under the platform row.

    The registry seed is re-applied first (idempotent), so a database whose
    registry rows were flushed still receives the platform ``video`` row.
    """
    seed_v2_capabilities(apps)
    capability_model = apps.get_model("providers", "ProviderCapability")
    version_model = apps.get_model("providers", "CapabilityVersion")
    capability = capability_model.objects.get(key="video", clinic_id=None)
    version_model.objects.get_or_create(
        capability=capability,
        **SYNTHETIC_VIDEO_VERSION,
        defaults={
            "state": "researched",
            "retention_terms": "synthetic: no media leaves the browser",
        },
    )
