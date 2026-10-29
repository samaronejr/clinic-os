"""Inspect pending protected signup captures only on the owner's controlling TTY."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    OwnerDatabaseCommand,
    owner_tty,
    required_uuid,
)
from apps.identity.signup import (
    SignupDeniedError,
    inspect_pending_signup,
    pending_signups,
)

if TYPE_CHECKING:
    from django.core.management import CommandParser


class Command(OwnerDatabaseCommand):
    """List opaque pending ids or inspect one capture without pipeable PII."""

    help = "Inspect pending signup registrations on the dedicated owner connection."

    def add_arguments(self, parser: CommandParser) -> None:
        """Offer only an opaque signup selector; secrets are never arguments."""
        parser.add_argument("--signup-id")

    def handle(self, *args: str, **options: CommandOption) -> None:
        """Open the controlling TTY before touching the owner database."""
        del args
        try:
            with owner_tty() as tty:
                if options.get("signup_id") is None:
                    for registration in pending_signups():
                        tty.write(f"{registration.pk}\n")
                    return
                capture = inspect_pending_signup(
                    signup_id=required_uuid(options, "signup_id")
                )
                tty.write(json.dumps(capture, ensure_ascii=False) + "\n")
        except (LifecycleCommandError, SignupDeniedError) as error:
            raise self.command_error() from error
