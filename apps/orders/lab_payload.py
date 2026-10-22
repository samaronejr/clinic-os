"""Closed signed callback schema; payload tenant claims are rejected, never read."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from apps.orders.lab import LabCallbackRejectedError
from apps.orders.values import (
    MAX_OBSERVATIONS,
    IdentityEvidence,
    ObservationInput,
    ResultInput,
)

if TYPE_CHECKING:
    from apps.audit.canonical import CanonicalValue


def _uuid(value: CanonicalValue) -> UUID:
    if not isinstance(value, str):
        raise LabCallbackRejectedError
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise LabCallbackRejectedError from error
    if str(parsed) != value:
        raise LabCallbackRejectedError
    return parsed


@dataclass(frozen=True, slots=True)
class LabPayload:
    """Only stored correlation supplies an order; no patient id is accepted."""

    correlation: UUID
    event: UUID
    identity: IdentityEvidence
    content_type: str
    data: bytes
    observations: tuple[ObservationInput, ...]
    critical: bool
    result_id: UUID | None
    predecessor_id: UUID | None

    def submission(self, *, order_id: UUID | None) -> ResultInput:
        """Adapt authenticated, parsed facts without widening lab authority."""
        return ResultInput(
            order_id=order_id,
            event_id=self.event,
            identity=self.identity,
            content_type=self.content_type,
            data=self.data,
            observations=self.observations,
            critical=self.critical,
            result_id=self.result_id,
            predecessor_id=self.predecessor_id,
        )


def parse_payload(value: CanonicalValue) -> LabPayload:
    """Parse once at the boundary; no expressions or arbitrary nested content."""
    if not isinstance(value, dict) or set(value) != {
        "correlation",
        "event_id",
        "identity",
        "content_type",
        "data_base64",
        "observations",
        "critical",
        "result_id",
        "predecessor_id",
    }:
        raise LabCallbackRejectedError
    identity = value["identity"]
    observations = value["observations"]
    if (
        not isinstance(identity, dict)
        or set(identity) != {"birth_date", "name", "identifier_kind", "identifier"}
        or not isinstance(observations, list)
        or len(observations) > MAX_OBSERVATIONS
        or type(value["critical"]) is not bool
        or not isinstance(value["content_type"], str)
        or not isinstance(value["data_base64"], str)
    ):
        raise LabCallbackRejectedError
    # The per-field narrowing keeps untrusted dictionaries out of domain values.
    born, name, kind, identifier = (
        identity["birth_date"],
        identity["name"],
        identity["identifier_kind"],
        identity["identifier"],
    )
    if (
        not isinstance(born, str)
        or not isinstance(name, str)
        or not isinstance(kind, str)
        or not isinstance(identifier, str)
    ):
        raise LabCallbackRejectedError
    try:
        birthday = date.fromisoformat(born) if born else None
        data = base64.b64decode(value["data_base64"], validate=True)
    except (ValueError, binascii.Error) as error:
        raise LabCallbackRejectedError from error
    if birthday is not None and birthday.isoformat() != born:
        raise LabCallbackRejectedError
    parsed_observations = []
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != {
            "code",
            "value",
            "unit",
            "reference_interval",
            "abnormal_flag",
        }:
            raise LabCallbackRejectedError
        code, measured, unit, interval, flag = (
            observation["code"],
            observation["value"],
            observation["unit"],
            observation["reference_interval"],
            observation["abnormal_flag"],
        )
        if (
            not isinstance(code, str)
            or not isinstance(measured, str)
            or not isinstance(unit, str)
            or not isinstance(interval, str)
            or not isinstance(flag, str)
        ):
            raise LabCallbackRejectedError
        parsed_observations.append(
            ObservationInput(
                code=code,
                value=measured,
                unit=unit,
                reference_interval=interval,
                abnormal_flag=flag,
            )
        )
    return LabPayload(
        _uuid(value["correlation"]),
        _uuid(value["event_id"]),
        IdentityEvidence(
            birth_date=birthday, name=name, identifier_kind=kind, identifier=identifier
        ),
        value["content_type"],
        data,
        tuple(parsed_observations),
        value["critical"],
        _uuid(value["result_id"]) if value["result_id"] is not None else None,
        _uuid(value["predecessor_id"]) if value["predecessor_id"] is not None else None,
    )
