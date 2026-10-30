"""Owner-CLI engineering evaluations; no real-world corpus or provider gate."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import DatabaseError, transaction

from apps.ai.contracts import (
    GatewayAccessDeniedError,
    ProviderUnavailableError,
    Purpose,
)
from apps.ai.evaluation.corpus import CAPABILITIES
from apps.ai.evaluation.protocol import ENGINEERING_THRESHOLDS, EvaluationInputError
from apps.ai.evaluation.report import build_report, report_html
from apps.ai.evaluation.runner import evaluate
from apps.ai.evaluation.services import (
    ReviewerAssignment,
    decide_change,
    evaluation_staff,
    finish_run,
    register_protocol,
    request_change,
    start_run,
    store_results,
)
from apps.ai.models import EvaluationProtocol
from apps.identity.management.base import (
    CommandOption,
    LifecycleCommandError,
    OwnerDatabaseCommand,
    assert_owner_database_role,
    required_text,
    required_uuid,
)
from apps.identity.management.context import (
    LifecycleContext,
    scoped_owner_gucs,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.core.management.base import CommandParser


@contextmanager
def _command_context(context: LifecycleContext) -> Iterator[None]:
    """Bind owner GUCs transaction-locally without assuming the runtime role."""
    with transaction.atomic(), scoped_owner_gucs(context):
        yield


class Command(OwnerDatabaseCommand):
    """Run offline fixtures under a named human's current clinic authority."""

    help = "Preregister, run, adjudicate and report synthetic engineering evaluations."

    def add_arguments(self, parser: CommandParser) -> None:
        """Accept only scope, immutable version identifiers and report destinations."""
        commands = parser.add_subparsers(dest="verb", required=True)
        for verb in ("register", "run", "finish", "report", "request-change", "decide"):
            command = commands.add_parser(verb)
            for name in ("organization-id", "clinic-id", "operator-id"):
                command.add_argument("--" + name, required=True)
            if verb in {"register", "run"}:
                command.add_argument(
                    "--capability", choices=CAPABILITIES, required=True
                )
                command.add_argument(
                    "--corpus", choices=("engineering-v1",), required=True
                )
            if verb == "register":
                command.add_argument("--clinical-owner-ref", required=True)
            elif verb == "run":
                command.add_argument("--configuration-id", required=True)
                for name in ("rater-one", "rater-two", "adjudicator"):
                    command.add_argument("--" + name, required=True)
            elif verb in {"finish", "report", "request-change"}:
                command.add_argument("--run-id", required=True)
                if verb == "report":
                    command.add_argument("--html")
            else:
                command.add_argument("--change-id", required=True)
                command.add_argument(
                    "--decision", choices=("approved", "rejected"), required=True
                )
                command.add_argument("--approval-ref", required=True)

    def handle(self, *args: str, **options: CommandOption) -> str:
        """Never reflect failed selectors, corpus content or environment credentials."""
        del args
        try:
            assert_owner_database_role()
            context = LifecycleContext(
                required_uuid(options, "operator_id"),
                required_uuid(options, "organization_id"),
                required_uuid(options, "clinic_id"),
            )
            return self._apply(context, options)
        except (
            LifecycleCommandError,
            EvaluationInputError,
            GatewayAccessDeniedError,
            ProviderUnavailableError,
            DatabaseError,
            ValueError,
            OSError,
        ):
            raise self.command_error() from None

    def _apply(
        self, context: LifecycleContext, options: dict[str, CommandOption]
    ) -> str:
        clinic = context.clinic_id
        verb = options["verb"]
        with _command_context(context):
            if verb == "register":
                protocol = register_protocol(
                    clinic_id=clinic,
                    capability=required_text(options, "capability"),
                    corpus_version=required_text(options, "corpus"),
                    thresholds=ENGINEERING_THRESHOLDS,
                    clinical_owner_ref=required_text(options, "clinical_owner_ref"),
                )
                return str(protocol.pk)
            if verb == "run":
                evaluation_staff(clinic_id=clinic, mode="clinical_owner")
                latest = (
                    EvaluationProtocol.objects.filter(
                        clinic_id=clinic,
                        capability=required_text(options, "capability"),
                        corpus_version=required_text(options, "corpus"),
                    )
                    .order_by("-version")
                    .first()
                )
                if latest is None:
                    raise EvaluationInputError
                run = start_run(
                    clinic_id=clinic,
                    protocol_id=latest.pk,
                    configuration_id=required_uuid(options, "configuration_id"),
                    reviewers=ReviewerAssignment(
                        required_uuid(options, "rater_one"),
                        required_uuid(options, "rater_two"),
                        required_uuid(options, "adjudicator"),
                    ),
                )
            elif verb == "finish":
                return finish_run(
                    clinic_id=clinic, run_id=required_uuid(options, "run_id")
                ).status
            elif verb == "request-change":
                return str(
                    request_change(
                        clinic_id=clinic, run_id=required_uuid(options, "run_id")
                    ).pk
                )
            elif verb == "decide":
                decision = required_text(options, "decision")
                if decision not in {"approved", "rejected"}:
                    raise EvaluationInputError
                return decide_change(
                    clinic_id=clinic,
                    change_id=required_uuid(options, "change_id"),
                    decision="approved" if decision == "approved" else "rejected",
                    approval_ref=required_text(options, "approval_ref"),
                ).status
            elif verb == "report":
                report = build_report(
                    clinic_id=clinic, run_id=required_uuid(options, "run_id")
                )
                destination = options.get("html")
                if isinstance(destination, str):
                    with Path(destination).open("x", encoding="utf-8") as stream:
                        stream.write(report_html(report))
                return json.dumps(report, indent=2, sort_keys=True)
            else:
                raise EvaluationInputError
        # The real shared provider boundary refuses calls inside a transaction.
        results = evaluate(
            capability=run.protocol.capability,
            corpus_version=run.protocol.corpus_version,
            provider_key=run.configuration.provider,
            model_id=run.configuration.model_id,
            purpose=Purpose(run.configuration.purpose),
        )
        with _command_context(context):
            stored = store_results(clinic_id=clinic, run_id=run.pk, results=results)
        return json.dumps({"run_id": str(stored.pk), "status": stored.status})
