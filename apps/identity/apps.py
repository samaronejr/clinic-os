"""Django configuration for the identity domain."""

from django.apps import AppConfig


class IdentityConfig(AppConfig):
    """Register the identity domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.identity"

    def ready(self) -> None:
        """Disable Django's login-time user-table write."""
        from django.contrib.auth.models import update_last_login  # noqa: PLC0415
        from django.contrib.auth.signals import user_logged_in  # noqa: PLC0415

        user_logged_in.disconnect(update_last_login, dispatch_uid="update_last_login")
