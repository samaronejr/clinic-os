"""Template helpers for the component library in templates/includes/components/."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django import template
from django.utils.translation import gettext_lazy as _

from apps.core.charts import TrendGeometry, trend_geometry

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from django.utils.functional import Promise

register = template.Library()


@dataclass(frozen=True)
class ComponentState:
    """One SC-8 state: the stable key and its display label."""

    key: str
    label: Promise


COMPONENT_STATES: Final = (
    ComponentState("default", _("Default")),
    ComponentState("hover", _("Hover")),
    ComponentState("focus", _("Focus")),
    ComponentState("active", _("Active")),
    ComponentState("disabled", _("Disabled")),
    ComponentState("loading", _("Loading")),
    ComponentState("error", _("Error")),
    ComponentState("success", _("Success")),
    ComponentState("empty", _("Empty")),
    ComponentState("stale", _("Stale")),
    ComponentState("conflict", _("Conflict")),
    ComponentState("permission-denied", _("Permission denied")),
    ComponentState("offline", _("Offline")),
)
# The six-state primitives predate SC-8; these are their added states.
PRIMITIVE_EXTRA_STATES: Final = tuple(
    state
    for state in COMPONENT_STATES
    if state.key
    in {"hover", "active", "empty", "stale", "conflict", "permission-denied", "offline"}
)


@register.simple_tag
def component_states() -> tuple[ComponentState, ...]:
    """Return every SC-8 state in documentation order."""
    return COMPONENT_STATES


@register.simple_tag
def primitive_extra_states() -> tuple[ComponentState, ...]:
    """Return the SC-8 states the original six primitives gained in v2."""
    return PRIMITIVE_EXTRA_STATES


@register.simple_tag
def trend_chart(
    series: Sequence[Mapping[str, object]],
    labels: Sequence[str],
    ref_low: object = None,
    ref_high: object = None,
) -> TrendGeometry:
    """Compute trend geometry for ``includes/components/chart.html``."""
    return trend_geometry(series, labels=labels, ref_low=ref_low, ref_high=ref_high)


@register.simple_tag
def diff_counts(lines: Sequence[Mapping[str, object]]) -> dict[str, int]:
    """Count added and removed lines for the diff caption."""
    kinds = [line.get("kind") for line in lines]
    return {"added": kinds.count("added"), "removed": kinds.count("removed")}
