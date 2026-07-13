"""Tenant-aware Django management command base."""

from uuid import UUID

from django.core.management import BaseCommand, CommandError
from django.core.management.base import CommandParser

from apps.tenancy.db import TenantAccessDeniedError, tenant_context

type CommandOption = str | int | bool | None | list[str]


def _required_uuid(raw_value: CommandOption, option_name: str) -> UUID:
    if not isinstance(raw_value, str):
        message = f"{option_name} is required"
        raise CommandError(message)
    try:
        return UUID(raw_value)
    except ValueError:
        message = f"{option_name} must be a UUID"
        raise CommandError(message) from None


class TenantCommand(BaseCommand):
    """Require explicit user and tenant identifiers around command work."""

    def add_arguments(self, parser: CommandParser) -> None:
        """Add the mandatory trusted-context inputs."""
        parser.add_argument("--user")
        parser.add_argument("--tenant")

    def handle(
        self,
        *args: str,
        **options: CommandOption,
    ) -> str | None:
        """Parse context before invoking tenant-scoped command behavior."""
        user_id = _required_uuid(options.get("user"), "--user")
        org_id = _required_uuid(options.get("tenant"), "--tenant")
        try:
            with tenant_context(user_id, org_id):
                return self.handle_tenant(*args, **options)
        except TenantAccessDeniedError:
            message = "tenant access denied"
            raise CommandError(message) from None

    def handle_tenant(
        self,
        *args: str,
        **options: CommandOption,
    ) -> str | None:
        """Implement tenant-scoped work in a concrete command."""
        raise NotImplementedError
