"""Generate an explicit template window through ordinary tenant authorization."""

from datetime import date
from typing import cast
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.resource_services import generate_availability
from apps.tenancy.db import tenant_context


class Command(BaseCommand):
    """A retryable job with no implicit horizon, timer, or elevated DB identity."""

    help = "Generate scheduling blocks for an authorized template and explicit dates."

    def add_arguments(self, parser: CommandParser) -> None:
        """Require trusted execution identity and explicit bounded scope."""
        for name in ("user-id", "organization-id", "clinic-id", "template-id"):
            parser.add_argument(f"--{name}", required=True, type=UUID)
        parser.add_argument("--start-date", required=True, type=date.fromisoformat)
        parser.add_argument("--end-date", required=True, type=date.fromisoformat)

    def handle(self, *args: object, **options: object) -> None:
        """Return only a count; IDs, clinical text and secrets never reach output."""
        del args
        try:
            with tenant_context(
                cast("UUID", options["user_id"]),
                cast("UUID", options["organization_id"]),
            ):
                blocks = generate_availability(
                    clinic_id=cast("UUID", options["clinic_id"]),
                    template_id=cast("UUID", options["template_id"]),
                    start_date=cast("date", options["start_date"]),
                    end_date=cast("date", options["end_date"]),
                )
        except (
            AppointmentAccessDeniedError,
            SchedulingRuleError,
            ValidationError,
            ValueError,
        ) as error:
            message = "Scheduling generation refused."
            raise CommandError(message) from error
        self.stdout.write(f"generated_blocks={len(blocks)}")
