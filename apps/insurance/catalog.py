"""Versioned operator, contract, price and terminology inputs."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from django.conf import settings

from apps.insurance.access import audit, clinic_write, fingerprint, replay, scoped
from apps.insurance.errors import InsuranceDeniedError, InsuranceInputError
from apps.insurance.limits import (
    MAX_CARD_LENGTH,
    MAX_CATALOG_TERMS,
    MAX_PAYER_NAME,
    MAX_PROCEDURE_QUANTITY,
    MAX_TERM_DESCRIPTION,
)
from apps.insurance.memberships import registered_membership
from apps.insurance.models import (
    InsuranceMembership,
    Payer,
    PayerContract,
    PriceTable,
    TussTerm,
)
from apps.insurance.tiss import MAX_XML_BYTES

if TYPE_CHECKING:
    from datetime import date


@dataclass(frozen=True, slots=True)
class ContractTerms:
    """An immutable payer contract revision."""

    version: int
    starts_on: date
    ends_on: date | None
    billing_identity: dict[str, object]
    channels: frozenset[str]


@dataclass(frozen=True, slots=True)
class MembershipTerms:
    """Explicit coverage start and newborn flag; intake supplies card and expiry."""

    registration_id: UUID
    valid_from: date
    newborn: str


def create_payer(
    *, clinic_id: UUID, ans_registry: str, name: str, command_id: UUID
) -> Payer:
    """Retain an operator identity without authorizing any external connectivity."""
    if (
        not re.fullmatch(r"[0-9]{6}", ans_registry, flags=re.ASCII)
        or not 1 <= len(name) <= MAX_PAYER_NAME
    ):
        raise InsuranceInputError
    digest = fingerprint([ans_registry, name])
    with clinic_write(clinic_id=clinic_id) as clinic:
        existing = replay(
            model=Payer, clinic_id=clinic_id, command_id=command_id, digest=digest
        )
        if existing is not None:
            return existing
        row = Payer.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            ans_registry=ans_registry,
            name=name,
        )
        audit(clinic_id=clinic_id, row=row, event="insurance.payer.created")
        return row


def create_contract(
    *,
    clinic_id: UUID,
    payer_id: UUID,
    terms: ContractTerms,
    command_id: UUID,
) -> PayerContract:
    """Version exact provider billing identities and supported payer operations."""
    version, starts_on, ends_on = terms.version, terms.starts_on, terms.ends_on
    billing_identity, channels = terms.billing_identity, terms.channels
    if (
        type(version) is not int
        or version < 1
        or (ends_on is not None and ends_on < starts_on)
        or not channels <= {"webservice", "eligibility", "authorization"}
        or set(billing_identity) != {"code", "name", "cnes", "professionals"}
    ):
        raise InsuranceInputError
    digest = fingerprint(
        [
            str(payer_id),
            version,
            str(starts_on),
            str(ends_on),
            billing_identity,
            sorted(channels),
        ]
    )
    with clinic_write(clinic_id=clinic_id) as clinic:
        scoped(model=Payer, clinic_id=clinic_id, identifier=payer_id)
        existing = replay(
            model=PayerContract,
            clinic_id=clinic_id,
            command_id=command_id,
            digest=digest,
        )
        if existing is not None:
            return existing
        row = PayerContract.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            payer_id=payer_id,
            version=version,
            starts_on=starts_on,
            ends_on=ends_on,
            billing_identity=billing_identity,
            webservice="webservice" in channels,
            eligibility_supported="eligibility" in channels,
            authorization_supported="authorization" in channels,
        )
        audit(clinic_id=clinic_id, row=row, event="insurance.contract.created")
        return row


def import_tuss(
    *,
    clinic_id: UUID,
    component_version: str,
    source_date: date,
    source: bytes,
    command_id: UUID,
) -> tuple[TussTerm, ...]:
    """Import a CSV component export with version, date and exact-source checksum."""
    if (
        component_version != settings.TISS_COMPONENT_VERSIONS["tuss"]
        or not source
        or len(source) > MAX_XML_BYTES
    ):
        raise InsuranceInputError
    try:
        reader = csv.DictReader(io.StringIO(source.decode("utf-8-sig")))
        if reader.fieldnames != ["table", "code", "description"]:
            raise InsuranceInputError
        terms = tuple(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise InsuranceInputError from error
    if not terms or len(terms) > MAX_CATALOG_TERMS:
        raise InsuranceInputError
    identities: set[tuple[str, str]] = set()
    for term in terms:
        if set(term) != {"table", "code", "description"} or not all(
            type(value) is str for value in term.values()
        ):
            raise InsuranceInputError
        if (
            not re.fullmatch(r"[0-9]{2}", term["table"], flags=re.ASCII)
            or not re.fullmatch(r"[0-9]{1,10}", term["code"], flags=re.ASCII)
            or not 1 <= len(term["description"]) <= MAX_TERM_DESCRIPTION
            or (term["table"], term["code"]) in identities
        ):
            raise InsuranceInputError
        identities.add((term["table"], term["code"]))
    checksum = sha256(source).hexdigest()
    rows: list[TussTerm] = []
    with clinic_write(clinic_id=clinic_id) as clinic:
        for index, term in enumerate(terms):
            identity = uuid5(command_id, str(index))
            digest = fingerprint([component_version, str(source_date), checksum, term])
            existing = replay(
                model=TussTerm, clinic_id=clinic_id, command_id=identity, digest=digest
            )
            if existing is not None:
                rows.append(existing)
                continue
            row = TussTerm.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                command_id=identity,
                fingerprint=digest,
                component_version=component_version,
                source_date=source_date,
                source_sha256=checksum,
                table_code=term["table"],
                code=term["code"],
                description=term["description"],
            )
            audit(clinic_id=clinic_id, row=row, event="insurance.tuss.imported")
            rows.append(row)
    return tuple(rows)


def create_price_table(
    *,
    clinic_id: UUID,
    contract_id: UUID,
    version: int,
    prices: dict[str, dict[str, object]],
    command_id: UUID,
) -> PriceTable:
    """Map actual scheduling service identities to imported terms and exact money."""
    if type(version) is not int or version < 1 or not prices:
        raise InsuranceInputError
    digest = fingerprint([str(contract_id), version, prices])
    with clinic_write(clinic_id=clinic_id) as clinic:
        scoped(model=PayerContract, clinic_id=clinic_id, identifier=contract_id)
        existing = replay(
            model=PriceTable, clinic_id=clinic_id, command_id=command_id, digest=digest
        )
        if existing is not None:
            return existing
        for service, terms in prices.items():
            if service != "unscheduled":
                try:
                    UUID(service)
                except ValueError as error:
                    raise InsuranceInputError from error
            if (
                set(terms) != {"table", "code", "quantity", "unit_minor"}
                or type(terms["quantity"]) is not int
                or not 1 <= terms["quantity"] <= MAX_PROCEDURE_QUANTITY
                or type(terms["unit_minor"]) is not int
                or not 1 <= terms["unit_minor"] <= 2**63 - 1
                or not TussTerm.objects.filter(
                    clinic_id=clinic_id,
                    component_version=settings.TISS_COMPONENT_VERSIONS["tuss"],
                    table_code=terms["table"],
                    code=terms["code"],
                ).exists()
            ):
                raise InsuranceInputError
        row = PriceTable.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            contract_id=contract_id,
            version=version,
            prices=prices,
        )
        audit(clinic_id=clinic_id, row=row, event="insurance.prices.created")
        return row


def register_membership(
    *,
    clinic_id: UUID,
    payer_id: UUID,
    enrollment_id: UUID,
    terms: MembershipTerms,
    command_id: UUID,
) -> InsuranceMembership:
    """Store patient card data only in an envelope, bound by the enrollment FK guard."""
    if terms.newborn not in {"S", "N"}:
        raise InsuranceInputError
    registration_id = terms.registration_id
    with clinic_write(clinic_id=clinic_id) as clinic:
        payer = scoped(model=Payer, clinic_id=clinic_id, identifier=payer_id)
        registration = registered_membership(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            registration_id=registration_id,
        )
        if (
            registration.payer_id is None
            or registration.ans_number != payer.ans_registry
        ):
            raise InsuranceDeniedError
        details = {
            "card": registration.membership_number or "",
            "newborn": terms.newborn,
        }
        valid_from, valid_to = terms.valid_from, registration.valid_until
        if not 1 <= len(details["card"]) <= MAX_CARD_LENGTH or (
            valid_to is not None and valid_to < valid_from
        ):
            raise InsuranceInputError
        digest = fingerprint(
            [
                str(payer_id),
                str(enrollment_id),
                str(registration_id),
                details,
                str(valid_from),
                str(valid_to),
            ]
        )
        existing = replay(
            model=InsuranceMembership,
            clinic_id=clinic_id,
            command_id=command_id,
            digest=digest,
        )
        if existing is not None:
            return existing
        row = InsuranceMembership.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            payer_id=payer_id,
            registration=registration,
            enrollment_id=enrollment_id,
            details=details,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        audit(clinic_id=clinic_id, row=row, event="insurance.membership.created")
        return row
