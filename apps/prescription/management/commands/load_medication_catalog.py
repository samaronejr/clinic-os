"""Load or refresh an explicitly staged, checksum-bound open-data snapshot."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.identity.management.base import CommandOption, LifecycleCommandError
from apps.prescription.catalog_loading import load_catalog_snapshot


class Command(BaseCommand):
    """Idempotent owner-run refresh job; no downloading or scheduled owner secrets."""

    help = "Load a normalized offline Anvisa/CMED catalog snapshot."

    def add_arguments(self, parser: CommandParser) -> None:
        """Require the exact local snapshot and its independently supplied digest."""
        parser.add_argument("--source", required=True)
        parser.add_argument("--sha256", required=True)

    def handle(self, *args: str, **options: CommandOption) -> str:
        """Validate provenance and schema before inserting any source rows."""
        del args
        source, checksum = options["source"], options["sha256"]
        if not isinstance(source, str) or not isinstance(checksum, str):
            reason = "catalog_arguments_invalid"
            raise CommandError(reason)
        try:
            load_catalog_snapshot(source=Path(source), checksum=checksum)
        except LifecycleCommandError:
            reason = "catalog_owner_connection_required"
            raise CommandError(reason) from None
        return "catalog_snapshot_loaded"
