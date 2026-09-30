"""Shell template tags: resolve the workspace once per rendered page."""

from typing import Any

from django import template

from apps.core.workspace import (
    DENIED_ATTRIBUTE,
    Workspace,
    is_auth_flow,
    resolve_workspace,
)

register = template.Library()


@register.simple_tag(takes_context=True)
def workspace_refusal(context: dict[str, Any]) -> str:
    """Enter read-only refusal rendering before resolving any workspace state."""
    request = context.get("request")
    if request is not None:
        setattr(request, DENIED_ATTRIBUTE, True)
    return ""


@register.simple_tag(takes_context=True)
def workspace_context(context: dict[str, Any]) -> Workspace | None:
    """Return the authenticated shell context, or ``None`` on focused screens."""
    request = context.get("request")
    if request is None or is_auth_flow(request):
        return None
    return resolve_workspace(request)
