"""Apply synthetic subscription provider receipts through the owner resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import DatabaseError, transaction

from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    OwnerDatabaseCommand,
    assert_owner_database_role,
    required_text,
    required_uuid,
)
from apps.identity.management.context import LifecycleContext, scoped_owner_gucs
from apps.subscriptions.collection import (
    CollectionUnavailableError,
    bootstrap_synthetic_collection,
)
from apps.subscriptions.services import (
    SubscriptionConflictError,
    receive_synthetic_event,
)

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(OwnerDatabaseCommand):
    """Use an owner DB connection and current organization-wide operator."""

    help = "Offline SaaS receipt rehearsal; real collection is BLOCKED-ON-EG (EG-18)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Require explicit owner intent and opaque scope; accept no payload."""
        commands = parser.add_subparsers(dest="verb", required=True)
        commands.add_parser("bootstrap-synthetic")
        receipt = commands.add_parser("apply")
        for name in (
            "operator-id",
            "organization-id",
            "clinic-id",
            "subscription-id",
            "event-id",
        ):
            receipt.add_argument("--" + name, required=True)
        receipt.add_argument(
            "--action",
            choices=("paid", "payment_failed", "advance", "offboard"),
            required=True,
        )

    def handle(self, *args: str, **options: CommandOption) -> str:
        """Return fixed outcomes only; never echo refused selectors."""
        del args
        try:
            assert_owner_database_role()
            if options["verb"] == "bootstrap-synthetic":
                bootstrap_synthetic_collection()
                return "synthetic; BLOCKED-ON-EG"
            context = LifecycleContext(
                required_uuid(options, "operator_id"),
                required_uuid(options, "organization_id"),
                required_uuid(options, "clinic_id"),
            )
            with transaction.atomic(), scoped_owner_gucs(context):
                result = receive_synthetic_event(
                    clinic_id=context.clinic_id,
                    subscription_id=required_uuid(options, "subscription_id"),
                    event_id=required_uuid(options, "event_id"),
                    action=required_text(options, "action"),
                )
                return result.state
        except (
            LifecycleCommandError,
            CollectionUnavailableError,
            SubscriptionConflictError,
            DatabaseError,
            ValueError,
        ):
            raise self.command_error() from None
