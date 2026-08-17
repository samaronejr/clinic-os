"""Revoke one guarded clinic staff role."""

from django.core.management.base import CommandParser

from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    owner_tty,
    required_text,
    required_uuid,
)
from apps.identity.management.owner_command import OwnerLifecycleCommand
from apps.identity.management.revocation import (
    RevokeStaffRoleRequest,
    revoke_staff_role,
)
from apps.identity.models import UserClinicRole


def _required_role(options: dict[str, CommandOption]) -> UserClinicRole.Role:
    value = required_text(options, "role")
    try:
        return UserClinicRole.Role(value)
    except ValueError as error:
        raise LifecycleCommandError from error


class Command(OwnerLifecycleCommand):
    """Revoke one exact role subject to lifecycle guards."""

    help = "Revoke one guarded clinic staff role."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register owner scope and exact target-role inputs."""
        self.add_owner_arguments(parser)
        parser.add_argument("--target-user-id")
        parser.add_argument("--role")

    def handle(
        self,
        *args: str,
        **options: CommandOption,
    ) -> str:
        """Verify the owner and revoke one guarded role."""
        del args
        try:
            with owner_tty() as tty:
                request = RevokeStaffRoleRequest(
                    target_user_id=required_uuid(options, "target_user_id"),
                    role=_required_role(options),
                )
                context = self.verified_context(options, tty)
                revoke_staff_role(context, request)
        except LifecycleCommandError:
            raise self.command_error() from None
        return "staff role revoked"
