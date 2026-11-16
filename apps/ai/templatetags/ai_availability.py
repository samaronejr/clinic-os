"""Manual fallback pages offer an AI control only while AI admits its purpose."""

from __future__ import annotations

from django import template

from apps.ai.contracts import GatewayRefusedError, Purpose
from apps.ai.policy import enabled_policy

register = template.Library()


@register.simple_tag
def ai_enabled(purpose: str) -> bool:
    """Check both kill switches and the purpose opt-in; the POST still rechecks."""
    try:
        enabled_policy(Purpose(purpose))
    except GatewayRefusedError:
        return False
    return True
