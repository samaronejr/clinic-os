"""Shared existing-tenant owner lifecycle command protocol."""

from io import TextIOWrapper

from django.core.management.base import CommandParser

from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    OwnerDatabaseCommand,
    hidden_input,
    required_uuid,
)
from apps.identity.management.context import LifecycleContext
from apps.identity.management.totp import (
    ManagementDevice,
    confirmed_device_preflight,
    verify_management_totp,
)


class OwnerLifecycleCommand(OwnerDatabaseCommand):
    """Enforce the non-argv management TOTP protocol for existing tenants."""

    def add_owner_arguments(self, parser: CommandParser) -> None:
        """Register the explicit operator and tenant scope."""
        for name in ("operator-id", "organization-id", "clinic-id"):
            parser.add_argument(f"--{name}")

    def verified_context(
        self,
        options: dict[str, CommandOption],
        tty: TextIOWrapper,
    ) -> LifecycleContext:
        """Preflight, select, prompt, and separately commit one current token."""
        context = LifecycleContext(
            operator_id=required_uuid(options, "operator_id"),
            organization_id=required_uuid(options, "organization_id"),
            clinic_id=required_uuid(options, "clinic_id"),
        )
        devices = confirmed_device_preflight(context)
        device = self._selected_device(devices, tty)
        token = hidden_input(tty, "Authentication code: ")
        verify_management_totp(context, device, token)
        return context

    def _selected_device(
        self,
        devices: tuple[ManagementDevice, ...],
        tty: TextIOWrapper,
    ) -> ManagementDevice:
        if len(devices) == 1:
            return devices[0]
        if not devices:
            raise LifecycleCommandError
        persistent_id = hidden_input(tty, "Authenticator ID: ")
        selected = tuple(
            device for device in devices if device.persistent_id == persistent_id
        )
        if len(selected) != 1:
            raise LifecycleCommandError
        return selected[0]
