"""Expose the signed-in user's theme and density to the shell template."""

from django import template

from apps.identity.preferences import UiPreferences, load_ui_preferences

register = template.Library()


@register.simple_tag
def ui_preferences() -> UiPreferences:
    """Return the current user's display preferences (defaults without a row)."""
    return load_ui_preferences()
