"""Shared teleconsult fixtures: the video registry rows rooms bind to.

Transactional tests flush the provider registry, and every new room must name
a ``video`` capability version. ``seed_video_versions`` rebuilds the
2026-09-24-v2 registry and the synthetic video version through the same
functions the migrations run; call it on the owner connection, outside
``runtime_role()``.
"""

from __future__ import annotations

import django.apps
from apps.teleconsult.migrations._provider_seed import seed_synthetic_video_version


def seed_video_versions() -> None:
    """Rebuild the capability registry and the synthetic video version."""
    seed_synthetic_video_version(django.apps.apps)
