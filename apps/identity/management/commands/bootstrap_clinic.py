"""Bootstrap the actorless first clinic and owner."""

from django.core.management.base import CommandParser

from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    OwnerDatabaseCommand,
    hidden_input,
    owner_tty,
    required_text,
    required_uuid,
)
from apps.identity.management.bootstrap import BootstrapRequest, bootstrap_clinic


class Command(OwnerDatabaseCommand):
    """Bootstrap the sole actorless first clinic and owner."""

    help = "Bootstrap the first clinic and owner."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register explicit organization, clinic, and owner inputs."""
        for name in (
            "organization-id",
            "organization-name",
            "cnpj",
            "clinic-id",
            "clinic-name",
            "crm-uf",
            "timezone",
            "owner-user-id",
            "owner-username",
            "owner-email",
        ):
            parser.add_argument(f"--{name}")

    def handle(
        self,
        *args: str,
        **options: CommandOption,
    ) -> str:
        """Run one actorless bootstrap with a hidden password."""
        del args
        try:
            with owner_tty() as tty:
                request = BootstrapRequest(
                    organization_id=required_uuid(options, "organization_id"),
                    organization_name=required_text(options, "organization_name"),
                    cnpj=required_text(options, "cnpj"),
                    clinic_id=required_uuid(options, "clinic_id"),
                    clinic_name=required_text(options, "clinic_name"),
                    crm_uf=required_text(options, "crm_uf"),
                    timezone=required_text(options, "timezone"),
                    owner_user_id=required_uuid(options, "owner_user_id"),
                    owner_username=required_text(options, "owner_username"),
                    owner_email=required_text(options, "owner_email"),
                )
                bootstrap_clinic(request, hidden_input(tty, "Password: "))
        except LifecycleCommandError:
            raise self.command_error() from None
        return "clinic bootstrapped"
