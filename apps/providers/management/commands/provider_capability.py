"""Owner CLI for the provider capability lifecycle.

Runs only as the ``clinic_owner`` database role
(``assert_owner_database_role``); the runtime role cannot reach any write
path. Approver identity is supplied explicitly on argv because these
commands are actorless: there is no staff session behind them. Denied
state transitions surface as the trigger's ``provider_transition_denied``
error; every other failure maps to the shared payload-free command error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import DatabaseError

from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    OwnerDatabaseCommand,
    assert_owner_database_role,
    required_text,
    required_uuid,
)
from apps.providers import lifecycle
from apps.providers.models import CapabilityVersion

if TYPE_CHECKING:
    from uuid import UUID

    from django.core.management.base import CommandParser

_TRANSITION_DENIED = "provider_transition_denied"


def _optional_text(options: dict[str, CommandOption], name: str) -> str:
    """Return one optional text option or the empty string."""
    value = options.get(name)
    return value if isinstance(value, str) else ""


def _optional_uuid(options: dict[str, CommandOption], name: str) -> UUID | None:
    """Return one optional UUID option or None."""
    value = options.get(name)
    if value is None:
        return None
    return required_uuid(options, name)


def _decision(options: dict[str, CommandOption]) -> lifecycle.ApprovalInput:
    """Collect the explicit accountable-approval arguments."""
    return lifecycle.ApprovalInput(
        approver_name=required_text(options, "approver_name"),
        approver_role=required_text(options, "approver_role"),
        evidence_uri=required_text(options, "evidence_uri"),
    )


class Command(OwnerDatabaseCommand):
    """Drive one provider capability through its lifecycle."""

    help = "Manage provider capability lifecycle records (owner only)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the subcommand verbs and their explicit inputs."""
        subparsers = parser.add_subparsers(dest="verb", required=True)

        propose = subparsers.add_parser("propose")
        for name in (
            "key",
            "provider",
            "account",
            "environment",
            "api-version",
            "region",
            "retention-terms",
            "record-ref",
            "description",
            "clinic-id",
        ):
            propose.add_argument(f"--{name}")
        propose.add_argument("--state", choices=sorted(lifecycle.PROPOSED_STATES))

        for verb in ("approve", "activate", "degrade", "revoke"):
            sub = subparsers.add_parser(verb)
            for name in (
                "key",
                "clinic-id",
                "approver-name",
                "approver-role",
                "evidence-uri",
            ):
                sub.add_argument(f"--{name}")
        for verb in ("degrade", "revoke"):
            subparsers.choices[verb].add_argument("--reason")

        report = subparsers.add_parser("report")
        report.add_argument("--markdown", action="store_true")

    def handle(self, *args: str, **options: CommandOption) -> str:
        """Dispatch one lifecycle verb under the owner role."""
        del args
        try:
            assert_owner_database_role()
            return self._dispatch(options)
        except LifecycleCommandError:
            raise self.command_error() from None
        except DatabaseError as error:
            if _TRANSITION_DENIED in str(error):
                raise self.command_error() from error
            raise

    def _dispatch(self, options: dict[str, CommandOption]) -> str:
        verb = options["verb"]
        if verb == "report":
            return (
                lifecycle.report_markdown()
                if options.get("markdown")
                else lifecycle.report_text()
            )
        key = required_text(options, "key")
        clinic_id = _optional_uuid(options, "clinic_id")
        if verb == "propose":
            version = lifecycle.propose_version(
                key,
                clinic_id=clinic_id,
                version_input=lifecycle.VersionInput(
                    provider=required_text(options, "provider"),
                    account=_optional_text(options, "account"),
                    environment=_optional_text(options, "environment"),
                    api_version=_optional_text(options, "api_version"),
                    region=_optional_text(options, "region"),
                    retention_terms=_optional_text(options, "retention_terms"),
                    state=_optional_text(options, "state")
                    or CapabilityVersion.State.RESEARCHED,
                    record_ref=_optional_text(options, "record_ref"),
                    description=_optional_text(options, "description"),
                ),
            )
        elif verb == "approve":
            version = lifecycle.approve_version(
                key, clinic_id=clinic_id, decision=_decision(options)
            )
        elif verb == "activate":
            version = lifecycle.activate_version(
                key, clinic_id=clinic_id, decision=_decision(options)
            )
        elif verb == "degrade":
            version = lifecycle.degrade_version(
                key,
                clinic_id=clinic_id,
                decision=_decision(options),
                reason=required_text(options, "reason"),
            )
        elif verb == "revoke":
            version = lifecycle.revoke_version(
                key,
                clinic_id=clinic_id,
                decision=_decision(options),
                reason=required_text(options, "reason"),
            )
        else:  # pragma: no cover - argparse enforces the verb set
            raise LifecycleCommandError
        return f"{key}: {version.state}"
