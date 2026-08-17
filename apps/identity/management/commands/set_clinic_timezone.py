"""Change one empty clinic's IANA timezone."""

from django.core.management.base import CommandParser

from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    owner_tty,
    required_text,
)
from apps.identity.management.owner_command import OwnerLifecycleCommand
from apps.identity.management.timezone_change import set_clinic_timezone


class Command(OwnerLifecycleCommand):
    """Change one empty clinic's validated IANA timezone."""

    help = "Change one empty clinic timezone."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register owner scope and the target timezone."""
        self.add_owner_arguments(parser)
        parser.add_argument("--timezone")

    def handle(
        self,
        *args: str,
        **options: CommandOption,
    ) -> str:
        """Verify the owner and change one eligible clinic timezone."""
        del args
        try:
            with owner_tty() as tty:
                timezone_key = required_text(options, "timezone")
                context = self.verified_context(options, tty)
                set_clinic_timezone(context, timezone_key)
        except LifecycleCommandError:
            raise self.command_error() from None
        return "clinic timezone changed"
