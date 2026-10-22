"""Display-boundary labels for Clinical Inbox items."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template

from apps.workflows.inbox import kind_label, owner_label

if TYPE_CHECKING:
    from apps.workflows.inbox import InboxItem

register = template.Library()


@register.filter
def inbox_owner(item: InboxItem) -> str:
    """Name the item's owner; a role-owned task names the role."""
    if item.owner == "role" and item.owner_label:
        return item.owner_label
    return owner_label(item.owner)


@register.filter
def inbox_kind(kind: str) -> str:
    """Name a registered feed kind."""
    return kind_label(kind)


@register.filter
def count_of(counts: dict[str, int], kind: str) -> int:
    """Read one feed's count from the page summary."""
    return counts.get(kind, 0)
