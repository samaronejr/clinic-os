"""Copy synthetic provider MAC secrets into the database (operator command)."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.comms.mac_secrets import provision_mac_secrets


class Command(BaseCommand):
    """Provision every missing synthetic account secret; fail on any gap."""

    help = "Copy synthetic provider MAC secrets into the database by COPY."

    def handle(self, *args: object, **options: object) -> None:  # noqa: ARG002
        """Refuse outside synthetic mode; report counts, never values."""
        if settings.CLINIC_DATA_MODE != "synthetic":
            message = "provider MAC secrets are provisioned only in synthetic mode"
            raise CommandError(message)
        added, missing = provision_mac_secrets()
        self.stdout.write(f"provider MAC secrets added: {added}")
        if missing:
            message = (
                f"{len(missing)} provider accounts have no secret in the secret"
                " store; their webhooks and provider answers stay refused"
                " until one is provisioned"
            )
            raise CommandError(message)
