"""Owner model configuration and global kill switch, never provider activation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import DatabaseError, connection, transaction

from apps.ai.contracts import GatewayInputError, Price, Purpose
from apps.ai.owner import (
    ModelTerms,
    propose_model,
    reset_circuit,
    set_global_switch,
    transition_model,
)
from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    OwnerDatabaseCommand,
    assert_owner_database_role,
    required_text,
    required_uuid,
)

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(OwnerDatabaseCommand):
    """Manage synthetic AI terms without marking an external gate satisfied."""

    help = "Configure synthetic AI models or change the global AI kill switch."

    def add_arguments(self, parser: CommandParser) -> None:
        """Accept closed subcommands, never a provider URL or credential."""
        commands = parser.add_subparsers(dest="verb", required=True)
        switch = commands.add_parser("global-switch")
        state = switch.add_mutually_exclusive_group(required=True)
        state.add_argument("--enable", action="store_true")
        state.add_argument("--disable", action="store_true")
        switch.add_argument("--approved-region", action="append", default=[])
        create = commands.add_parser("model-create")
        create.add_argument(
            "--purpose", choices=[p.value for p in Purpose], required=True
        )
        create.add_argument(
            "--provider", choices=["synthetic", "recorded_fixture"], default="synthetic"
        )
        create.add_argument(
            "--route", choices=["primary", "fallback"], default="primary"
        )
        for name in ("model-id", "region", "retention-terms", "training-terms"):
            create.add_argument("--" + name, required=True)
        for name in ("input-micros", "output-micros", "cached-micros", "audio-micros"):
            create.add_argument("--" + name, type=int, required=True)
        transition = commands.add_parser("model-state")
        transition.add_argument("--configuration-id", required=True)
        transition.add_argument(
            "--state", choices=["approved", "active", "retired"], required=True
        )
        transition.add_argument("--approval-ref", default="")
        reset = commands.add_parser("circuit-reset")
        for name in ("organization-id", "clinic-id", "configuration-id"):
            reset.add_argument("--" + name, required=True)

    def handle(self, *args: str, **options: CommandOption) -> str:
        """Return only non-content status and safe configuration references."""
        del args
        try:
            assert_owner_database_role()
            return self._apply(options)
        except (LifecycleCommandError, GatewayInputError, DatabaseError, ValueError):
            raise self.command_error() from None

    def _apply(self, options: dict[str, CommandOption]) -> str:
        match options["verb"]:
            case "global-switch":
                regions = options["approved_region"]
                if not isinstance(regions, list):
                    raise GatewayInputError
                set_global_switch(
                    enabled=options["enable"] is True, approved_regions=regions
                )
                return "AI global switch changed; synthetic only."
            case "model-create":
                rates: list[int] = []
                for key in (
                    "input_micros",
                    "output_micros",
                    "cached_micros",
                    "audio_micros",
                ):
                    value = options[key]
                    if type(value) is not int:
                        raise GatewayInputError
                    rates.append(value)
                provider = required_text(options, "provider")
                route = required_text(options, "route")
                if provider not in {"synthetic", "recorded_fixture"} or (
                    route not in {"primary", "fallback"}
                ):
                    raise GatewayInputError
                model = propose_model(
                    terms=ModelTerms(
                        purpose=Purpose(required_text(options, "purpose")),
                        provider="synthetic"
                        if provider == "synthetic"
                        else "recorded_fixture",
                        model_id=required_text(options, "model_id"),
                        region=required_text(options, "region"),
                        retention_terms=required_text(options, "retention_terms"),
                        training_terms=required_text(options, "training_terms"),
                        price=Price(*rates),
                        route="primary" if route == "primary" else "fallback",
                    )
                )
                return str(model.pk)
            case "model-state":
                state = required_text(options, "state")
                match state:
                    case "approved" | "active" | "retired":
                        model = transition_model(
                            configuration_id=required_uuid(options, "configuration_id"),
                            state=state,
                            approval_ref=str(options["approval_ref"]),
                        )
                    case _:
                        raise GatewayInputError
                return str(model.pk)
            case "circuit-reset":
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('app.current_tenant',%s,true)",
                        [str(required_uuid(options, "organization_id"))],
                    )
                    return str(
                        reset_circuit(
                            clinic_id=required_uuid(options, "clinic_id"),
                            configuration_id=required_uuid(options, "configuration_id"),
                        )
                    )
            case _:
                raise GatewayInputError
