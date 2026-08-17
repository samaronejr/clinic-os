"""Provision one new clinic staff identity and role."""

from django.core.management.base import CommandParser

from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    hidden_input,
    owner_tty,
    required_text,
    required_uuid,
)
from apps.identity.management.owner_command import OwnerLifecycleCommand
from apps.identity.management.provisioning import (
    ProvisionStaffRequest,
    provision_staff,
)
from apps.identity.models import UserClinicRole


def _required_role(options: dict[str, CommandOption]) -> UserClinicRole.Role:
    value = required_text(options, "role")
    try:
        return UserClinicRole.Role(value)
    except ValueError as error:
        raise LifecycleCommandError from error


class Command(OwnerLifecycleCommand):
    """Provision one new canonical clinic staff identity."""

    help = "Provision one new clinic staff identity."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register owner scope and explicit staff identity inputs."""
        self.add_owner_arguments(parser)
        for name in ("staff-user-id", "username", "email", "role"):
            parser.add_argument(f"--{name}")

    def handle(
        self,
        *args: str,
        **options: CommandOption,
    ) -> str:
        """Verify the owner and provision staff with a hidden password."""
        del args
        try:
            with owner_tty() as tty:
                request = ProvisionStaffRequest(
                    user_id=required_uuid(options, "staff_user_id"),
                    username=required_text(options, "username"),
                    email=required_text(options, "email"),
                    role=_required_role(options),
                )
                context = self.verified_context(options, tty)
                password = hidden_input(tty, "Password: ")
                provision_staff(context, request, password)
        except LifecycleCommandError:
            raise self.command_error() from None
        return "staff provisioned"
