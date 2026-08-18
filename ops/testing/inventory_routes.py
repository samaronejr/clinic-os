"""Produce a deterministic inventory of Django route templates and names."""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django
from django.urls import URLPattern, URLResolver, get_resolver


def inventory_routes() -> tuple[str, ...]:
    """Return sorted route-pattern and route-name records without requests."""
    django.setup()
    records: list[str] = []
    _walk(get_resolver().url_patterns, "", records)
    return tuple(sorted(set(records)))


def _walk(
    patterns: list[URLPattern | URLResolver],
    prefix: str,
    records: list[str],
) -> None:
    for pattern in patterns:
        route = f"{prefix}{pattern.pattern}"
        if isinstance(pattern, URLPattern):
            records.append(f"{route}|{pattern.name or ''}")
        else:
            _walk(pattern.url_patterns, route, records)
