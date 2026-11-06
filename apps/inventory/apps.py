"""Inventory application registration."""

from django.apps import AppConfig


class InventoryConfig(AppConfig):
    """Keep stock accounting independent from treatment decisions."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
