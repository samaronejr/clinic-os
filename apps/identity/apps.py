"""Django configuration for the identity domain."""

from django.apps import AppConfig


class IdentityConfig(AppConfig):
    """Register the identity domain with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.identity"

    def ready(self) -> None:
        """Register login behavior that never writes the user table."""
        from django.contrib.auth.models import update_last_login  # noqa: PLC0415
        from django.contrib.auth.signals import user_logged_in  # noqa: PLC0415

        from apps.identity.services import (  # noqa: PLC0415
            set_active_organization_on_login,
        )

        user_logged_in.disconnect(update_last_login, dispatch_uid="update_last_login")
        user_logged_in.connect(
            set_active_organization_on_login,
            dispatch_uid="identity.set_active_organization_on_login",
        )
