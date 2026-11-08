"""Source-backed insurance guides; billing cannot change the underlying chart."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import TYPE_CHECKING, cast

from django.conf import settings
from django.db import connection

from apps.insurance.access import audit, clinic_write, fingerprint, replay, scoped
from apps.insurance.errors import InsuranceDeniedError, InsuranceInputError
from apps.insurance.models import (
    Guide,
    InsuranceMembership,
    PayerContract,
    PriceTable,
    TussTerm,
)
from apps.insurance.tiss import generate_batch
from apps.insurance.tiss_types import BatchFacts, GuideFacts, Procedure, Professional
from apps.insurance.transitions import transition

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class GuideSelection:
    """Record selectors carry no caller-supplied clinician or clinical narrative."""

    encounter_id: UUID
    invoice_id: UUID
    membership_id: UUID
    price_table_id: UUID
    kind: str
    principal_number: str = ""
    predecessor_id: UUID | None = None


def stored_facts(guide: Guide) -> GuideFacts:
    """Decode only a previously schema-validated immutable snapshot."""
    data = cast("dict[str, object]", guide.facts)
    return GuideFacts(
        number=guide.number,
        kind=guide.kind,
        registry=cast("str", data["registry"]),
        card=cast("str", data["card"]),
        provider_code=cast("str", data["provider_code"]),
        provider_name=cast("str", data["provider_name"]),
        cnes=cast("str", data["cnes"]),
        performed_on=date.fromisoformat(cast("str", data["performed_on"])),
        professional=Professional(**cast("dict[str, str]", data["professional"])),
        procedures=tuple(
            Procedure(
                table=cast("str", item["table"]),
                code=cast("str", item["code"]),
                description=cast("str", item["description"]),
                quantity=cast("int", item["quantity"]),
                unit_minor=cast("int", item["unit_minor"]),
            )
            for item in cast("list[dict[str, object]]", data["procedures"])
        ),
        principal_number=cast("str", data["principal_number"]),
        newborn=cast("str", data["newborn"]),
    )


def draft_guide(
    *, clinic_id: UUID, selection: GuideSelection, command_id: UUID
) -> Guide:
    """Bind completed care, insurer charges, patient membership and exact prices."""
    digest = fingerprint(
        [
            str(selection.encounter_id),
            str(selection.invoice_id),
            str(selection.membership_id),
            str(selection.price_table_id),
            selection.kind,
            selection.principal_number,
            str(selection.predecessor_id),
        ]
    )
    with clinic_write(clinic_id=clinic_id) as clinic:
        existing = replay(
            model=Guide, clinic_id=clinic_id, command_id=command_id, digest=digest
        )
        if existing is not None:
            return existing
        prices = scoped(
            model=PriceTable, clinic_id=clinic_id, identifier=selection.price_table_id
        )
        contract = scoped(
            model=PayerContract, clinic_id=clinic_id, identifier=prices.contract_id
        )
        membership = scoped(
            model=InsuranceMembership,
            clinic_id=clinic_id,
            identifier=selection.membership_id,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.insurance_guide_source(%s,%s,%s,%s)",
                [
                    clinic_id,
                    selection.encounter_id,
                    selection.invoice_id,
                    membership.pk,
                ],
            )
            source = cursor.fetchone()
        if source is None or membership.payer_id != contract.payer_id:
            raise InsuranceDeniedError
        performed_on = cast("date", source[3])
        if (
            performed_on < membership.valid_from
            or (membership.valid_to is not None and performed_on > membership.valid_to)
            or performed_on < contract.starts_on
            or (contract.ends_on is not None and performed_on > contract.ends_on)
        ):
            raise InsuranceInputError
        mapping = cast("dict[str, dict[str, object]]", prices.prices)
        key = str(source[2]) if source[2] is not None else "unscheduled"
        if key not in mapping:
            raise InsuranceInputError
        price = mapping[key]
        term = TussTerm.objects.filter(
            clinic_id=clinic_id,
            component_version=settings.TISS_COMPONENT_VERSIONS["tuss"],
            table_code=price["table"],
            code=price["code"],
        ).first()
        if term is None:
            raise InsuranceInputError
        total = cast("int", price["quantity"]) * cast("int", price["unit_minor"])
        if total != source[4] or selection.kind not in {
            "consulta",
            "spsadt",
            "honorarios",
        }:
            raise InsuranceInputError
        identity = cast("dict[str, object]", contract.billing_identity)
        professionals = cast("dict[str, dict[str, str]]", identity["professionals"])
        professional = professionals.get(str(source[1]))
        if professional is None or set(professional) != {
            "name",
            "council",
            "number",
            "uf",
            "cbo",
        }:
            raise InsuranceInputError
        if not all(type(value) is str for value in professional.values()):
            raise InsuranceInputError
        facts: dict[str, object] = {
            "registry": contract.payer.ans_registry,
            "card": membership.details["card"],
            "newborn": membership.details["newborn"],
            "provider_code": identity["code"],
            "provider_name": identity["name"],
            "cnes": identity["cnes"],
            "performed_on": performed_on.isoformat(),
            "professional": asdict(Professional(**professional)),
            "procedures": [
                {
                    "table": term.table_code,
                    "code": term.code,
                    "description": term.description,
                    "quantity": price["quantity"],
                    "unit_minor": price["unit_minor"],
                }
            ],
            "principal_number": selection.principal_number,
        }
        row = Guide.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            contract=contract,
            membership=membership,
            encounter_id=selection.encounter_id,
            invoice_id=selection.invoice_id,
            price_table=prices,
            predecessor_id=selection.predecessor_id,
            number=str(Guide.objects.filter(clinic_id=clinic_id).count() + 1),
            kind=selection.kind,
            facts=facts,
            total_minor=total,
        )
        audit(clinic_id=clinic_id, row=row, event="insurance.guide.draft")
        return row


def validate_guide(*, clinic_id: UUID, guide_id: UUID, command_id: UUID) -> Guide:
    """Advance only when the actual generated message passes the ANS XSD."""
    with clinic_write(clinic_id=clinic_id) as clinic:
        row = scoped(model=Guide, clinic_id=clinic_id, identifier=guide_id)
        registered_at = row.created_at
        generate_batch(
            facts=BatchFacts("1", registered_at, (stored_facts(row),)),
            versions=settings.TISS_COMPONENT_VERSIONS,
        )
        transition(clinic=clinic, row=row, target="validated", command_id=command_id)
        return row


def resubmit_guide(*, clinic_id: UUID, guide_id: UUID, command_id: UUID) -> Guide:
    """Retain the old sealed version; a resubmission has a new number and lineage."""
    with clinic_write(clinic_id=clinic_id):
        old = scoped(model=Guide, clinic_id=clinic_id, identifier=guide_id)
        return draft_guide(
            clinic_id=clinic_id,
            selection=GuideSelection(
                old.encounter_id,
                old.invoice_id,
                old.membership_id,
                old.price_table_id,
                old.kind,
                stored_facts(old).principal_number,
                old.pk,
            ),
            command_id=command_id,
        )
