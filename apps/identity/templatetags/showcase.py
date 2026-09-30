"""Tags used only by the DEBUG-only identity/showcase.html review page."""

from django import template

from apps.identity.showcase_data import Fixture
from apps.identity.showcase_data import showcase_fixture as load_fixture

register = template.Library()


@register.simple_tag
def showcase_fixture(name: str) -> Fixture:
    """Expose one named synthetic fixture to the showcase template."""
    return load_fixture(name)
