"""Export offline provider comparisons and optional synthetic lifecycle records."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import DatabaseError, transaction

from apps.ai.benchmark import benchmark, markdown_report, record_selections
from apps.ai.evaluation.protocol import EvaluationInputError
from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    OwnerDatabaseCommand,
    assert_owner_database_role,
    required_text,
)

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(OwnerDatabaseCommand):
    """No provider, endpoint, corpus file or activation switch is accepted."""

    help = "Benchmark offline adapters; real providers are BLOCKED-ON-EG."

    def add_arguments(self, parser: CommandParser) -> None:
        """Export a report; lifecycle writes require the existing owner role."""
        parser.add_argument("--report", required=True)
        parser.add_argument("--record-selections", action="store_true")

    def handle(self, *args: str, **options: CommandOption) -> str:
        """Refuse overwrites and redact every failure at the command boundary."""
        del args
        created = False
        destination = required_text(options, "report")
        path = Path(destination)
        try:
            publish = bool(options["record_selections"])
            if publish:
                assert_owner_database_role()
            report = benchmark()
            with path.open("x", encoding="utf-8") as stream:
                created = True
                with transaction.atomic() if publish else nullcontext():
                    if publish:
                        record_selections(report, record_ref=destination)
                    stream.write(markdown_report(report))
                    stream.flush()
        except (
            EvaluationInputError,
            LifecycleCommandError,
            DatabaseError,
            OSError,
        ):
            if created:
                path.unlink()
            raise self.command_error() from None
        return "synthetic benchmark complete; provider legs BLOCKED-ON-EG"
