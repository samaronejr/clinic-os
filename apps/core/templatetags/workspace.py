"""Shell template tags: resolve the workspace once per rendered page."""

from typing import Any

from django import template

from apps.core.workspace import Workspace, is_auth_flow, resolve_workspace

register = template.Library()


@register.simple_tag(takes_context=True)
def workspace_context(context: dict[str, Any]) -> Workspace | None:
    """Return the authenticated shell context, or ``None`` on focused screens."""
    request = context.get("request")
    if request is None or is_auth_flow(request):
        return None
    return resolve_workspace(request)
