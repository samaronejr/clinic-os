"""Authorized immutable financial snapshots, reloaded at every disclosure/effect."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from apps.ai.action_contracts import ActionInputError
from apps.ai.action_payload import JSON, closed, digest_text, identifier
from apps.ai.contracts import GatewayAccessDeniedError, GatewayInputError
from apps.ai.revenue_contracts import MAX_ROWS, RevenueFacts, decode, parse_model_input
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.models import InvoiceTerms, JournalEntry, SettlementAllocation
from apps.billing.security import book_write
from apps.identity.current_context import CurrentActorError
from apps.insurance.access import authority, clinic_write, scoped
from apps.insurance.errors import InsuranceDeniedError
from apps.insurance.guides import stored_facts
from apps.insurance.models import Glosa, Guide

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

CONTEXT_SCHEMA = "revenue-context-v1"


def require_finance(*, clinic_id: UUID) -> UUID:
    """Claims and journal authority must both hold in this exact clinic."""
    try:
        authority(clinic_id=clinic_id, permission="charge.read")
        authority(clinic_id=clinic_id, permission="tiss.read")
        return authority(clinic_id=clinic_id, permission="tiss.manage")
    except InsuranceDeniedError:
        raise GatewayAccessDeniedError from None


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RevenueContext:
    """Opaque source bindings stay encrypted; only ``facts`` enters inference."""

    guide_id: UUID
    sources: tuple[dict[str, JSON], ...] = field(repr=False)
    facts: RevenueFacts = field(repr=False)

    def encode(self) -> str:
        """Retain a complete, exact snapshot in the existing protected artifact."""
        return json.dumps(
            {
                "schema": CONTEXT_SCHEMA,
                "guide_id": str(self.guide_id),
                "sources": list(self.sources),
                "model": decode(self.facts.encode()),
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def parse_context(raw: str) -> RevenueContext:
    """No caller can add a patient selector, clinical narrative or raw payer text."""
    try:
        root = closed(decode(raw), {"schema", "guide_id", "sources", "model"})
        if root["schema"] != CONTEXT_SCHEMA:
            raise GatewayInputError
        guide_id = identifier(root["guide_id"])
        sources = root["sources"]
        if not isinstance(sources, list) or len(sources) > 2 * MAX_ROWS + 3:
            raise GatewayInputError
        parsed: list[dict[str, JSON]] = []
        for raw_source in sources:
            source = closed(raw_source, {"alias", "kind", "id", "version", "sha256"})
            identifier(source["id"])
            digest_text(source["sha256"])
            if (
                source["kind"] not in {"guide", "glosa", "journal", "allocation"}
                or source["version"] != 1
                or not isinstance(source["alias"], str)
            ):
                raise GatewayInputError
            parsed.append(source)
        facts = parse_model_input(json.dumps(root["model"]))
    except ActionInputError:
        raise GatewayInputError from None
    return RevenueContext(guide_id, tuple(parsed), facts)


def _source(alias: str, kind: str, identity: UUID, value: object) -> dict[str, JSON]:
    return {
        "alias": alias,
        "kind": kind,
        "id": str(identity),
        "version": 1,
        "sha256": _digest(value),
    }


def _bounded[T](rows: Iterable[T]) -> tuple[T, ...]:
    values = tuple(rows)
    if len(values) > MAX_ROWS:
        raise GatewayInputError
    return values


def retrieve_context(*, clinic_id: UUID, guide_id: UUID) -> RevenueContext:
    """RLS and current finance authority precede every source read.

    Insurance and journal writers take these same locks. They remain held until
    the caller's outer transaction commits, including throughout A1 approval.
    Callers that also need the gateway budget or agent send lock take it first.
    Guide procedure facts were bound to the actual completed encounter by the
    insurance service/trigger; this reader never opens the chart or membership.
    """
    require_finance(clinic_id=clinic_id)
    try:
        with (
            clinic_write(clinic_id=clinic_id),
            book_write(clinic_id=clinic_id, permission="charge.read") as book,
        ):
            guide = scoped(model=Guide, clinic_id=clinic_id, identifier=guide_id)
            if Guide.objects.filter(clinic_id=clinic_id, predecessor=guide).exists():
                raise GatewayAccessDeniedError
            procedures: list[JSON] = [
                {
                    "table": p.table,
                    "code": p.code,
                    "quantity": p.quantity,
                    "unit_minor": p.unit_minor,
                }
                for p in stored_facts(guide).procedures
            ]
            sources = [
                _source(
                    "G1",
                    "guide",
                    guide.pk,
                    [guide.fingerprint, guide.state, procedures, guide.total_minor],
                )
            ]
            latest = (
                Glosa.objects.filter(clinic_id=clinic_id, guide=guide)
                .order_by("-source__created_at", "-source_id")
                .first()
            )
            glosas = (
                _bounded(
                    Glosa.objects.filter(
                        clinic_id=clinic_id, guide=guide, source_id=latest.source_id
                    ).order_by("sequence", "id")[: MAX_ROWS + 1]
                )
                if latest is not None
                else ()
            )
            returns: list[dict[str, JSON]] = []
            for position, row in enumerate(glosas, 1):
                alias = f"R{position}"
                returns.append(
                    {
                        "source": alias,
                        "sequence": row.sequence,
                        "charged_minor": row.charged_minor,
                        "accepted_minor": row.accepted_minor,
                        "denied_minor": row.denied_minor,
                        "code": row.code,
                    }
                )
                sources.append(_source(alias, "glosa", row.pk, row.fingerprint))
            terms = (
                InvoiceTerms.objects.select_related("completion_entry")
                .filter(book=book, invoice_id=guide.invoice_id)
                .first()
            )
            recognized = False
            if terms is not None:
                entry = terms.completion_entry
                recognized = (
                    entry is not None
                    and entry.state == "posted"
                    and not JournalEntry.objects.filter(
                        book=book, reversal_of=entry, state="posted"
                    ).exists()
                )
                sources.append(
                    _source(
                        "J1",
                        "journal",
                        terms.pk,
                        [
                            terms.net_minor,
                            str(terms.completion_entry_id),
                            recognized,
                            bytes(entry.content_hash).hex() if entry else None,
                        ],
                    )
                )
            else:
                sources.append(
                    _source(
                        "J1",
                        "journal",
                        book.pk,
                        ["terms_not_found", str(guide.invoice_id)],
                    )
                )
            allocations = _bounded(
                SettlementAllocation.objects.filter(
                    book=book,
                    terms__invoice_id=guide.invoice_id,
                    settlement__state="posted",
                    settlement__entry__state="posted",
                    entry__state="posted",
                ).order_by("id")[: MAX_ROWS + 1]
            )
            sources.extend(
                _source(
                    "P1", "allocation", row.pk, [row.amount_minor, str(row.entry_id)]
                )
                for row in allocations
            )
            # Empty evidence is a scoped query result, not an invented receipt.
            # Retain the book/invoice query and exact bounded result so P1 still
            # resolves when no posted allocation exists.
            sources.append(
                _source(
                    "P1",
                    "journal",
                    book.pk,
                    [
                        "posted_allocation_coverage",
                        str(guide.invoice_id),
                        [
                            [str(row.pk), row.amount_minor, str(row.entry_id)]
                            for row in allocations
                        ],
                    ],
                )
            )
            model = {
                "schema": "revenue-cycle-v1",
                "guide": {
                    "source": "G1",
                    "total_minor": guide.total_minor,
                    "procedures": procedures,
                },
                "journal": {
                    "source": "J1",
                    "net_minor": terms.net_minor if terms else None,
                    "recognized": recognized,
                },
                "returns": returns,
                "payments": {
                    "source": "P1",
                    "allocated_minor": sum(row.amount_minor for row in allocations),
                },
            }
            return RevenueContext(
                guide.pk, tuple(sources), parse_model_input(json.dumps(model))
            )
    except (InsuranceDeniedError, BillingAccessDeniedError, CurrentActorError):
        raise GatewayAccessDeniedError from None


def recheck_context(*, clinic_id: UUID, content: str) -> RevenueContext:
    """Invalidate changed sources, new returns/allocations or a superseding guide."""
    expected = parse_context(content)
    current = retrieve_context(clinic_id=clinic_id, guide_id=expected.guide_id)
    if current.encode() != expected.encode():
        raise GatewayAccessDeniedError
    return current
