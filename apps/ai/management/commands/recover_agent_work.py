"""Republish retained work through the exact authenticated machine's queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.ai.execution_tasks import recover_approved
from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    required_uuid,
)
from apps.tenancy.db import ServicePrincipalAccessDeniedError

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    """Queue opaque references; only the authenticated worker can act on them."""

    help = "Recover pending agent work for one clinic and authenticated machine."

    def add_arguments(self, parser: CommandParser) -> None:
        """Accept scope selectors, never a provider key, command or outcome."""
        parser.add_argument("--clinic-id", required=True)
        parser.add_argument("--principal-id", required=True)

    def handle(self, *args: str, **options: CommandOption) -> str:
        """Authenticate before enumeration and disclose only a bounded count."""
        del args
        try:
            count = recover_approved(
                clinic_id=required_uuid(options, "clinic_id"),
                principal_id=required_uuid(options, "principal_id"),
            )
        except (
            LifecycleCommandError,
            ServicePrincipalAccessDeniedError,
            DatabaseError,
        ):
            reason = "agent recovery unavailable"
            raise CommandError(reason) from None
        return f"Queued agent work: {count}"
