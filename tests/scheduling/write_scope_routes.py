"""Resolver-derived appointment routes, with closed sibling classifications."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence


@dataclass(frozen=True, slots=True)
class WriteRoute:
    name: str
    modes: tuple[str, ...]
    selector: str


# These siblings have their own HTTP suites; a new callback cannot silently
# fall outside the appointment matrix. GET-only callbacks are classified too.
SIBLINGS = {
    "resource_settings": "scheduling.test_resource_http",
    "patient_booking_view": "renewal.test_self_booking",
    "patient_offers_view": "renewal.test_waitlist",
    "waitlist_view": "renewal.test_waitlist",
    "availability_list_view": "scheduling.test_availability_http",
    "availability_retire_view": "scheduling.test_availability_http",
    "agenda_view": "scheduling.test_agenda_http",
}


def _leaves(
    patterns: Sequence[URLPattern | URLResolver], namespaces: tuple[str, ...] = ()
) -> Iterator[tuple[str, URLPattern]]:
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            nested = (
                (*namespaces, pattern.namespace) if pattern.namespace else namespaces
            )
            yield from _leaves(pattern.url_patterns, nested)
        else:
            callback = inspect.unwrap(pattern.callback)
            if callback.__module__.startswith("apps.scheduling."):
                assert pattern.name is not None, pattern.pattern
                yield ":".join((*namespaces, pattern.name)), pattern


def _booking_modes(callback: Callable[..., object]) -> tuple[str, ...]:
    """Derive the dispatch's admitted modes rather than copying their values."""
    module = inspect.getmodule(callback)
    assert module is not None
    tree = ast.parse(inspect.getsource(callback))
    decisions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "mode"
        and any(isinstance(operation, ast.NotIn) for operation in node.ops)
    ]
    assert len(decisions) == 1, "unclassified booking mode decision"
    admitted = decisions[0].comparators[0]
    assert isinstance(admitted, ast.Set), "unclassified booking mode vocabulary"
    modes = []
    for value in admitted.elts:
        assert isinstance(value, ast.Name), "unclassified booking mode value"
        mode = getattr(module, value.id)
        assert isinstance(mode, str)
        modes.append(mode)
    return tuple(sorted(modes))


def write_routes() -> tuple[WriteRoute, ...]:
    """Classify every live scheduling callback and exercise every appointment alias."""
    result = []
    for name, pattern in _leaves(get_resolver().url_patterns):
        callback = inspect.unwrap(pattern.callback)
        match callback.__name__:
            case "appointment_create_view":
                result.append(WriteRoute(name, _booking_modes(callback), "clinic_id"))
            case "appointment_reschedule_view":
                result.append(WriteRoute(name, ("reschedule",), "appointment_id"))
            case "appointment_cancel_view":
                result.append(WriteRoute(name, ("cancel",), "appointment_id"))
            case sibling:
                assert sibling in SIBLINGS, ("unclassified scheduling route", name)
    assert result, "no scheduling appointment routes discovered"
    return tuple(result)
