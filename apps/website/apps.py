"""Website application wiring."""

from django.apps import AppConfig


class WebsiteConfig(AppConfig):
    """Serve the public product pages and help center; no models, no services."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.website"
