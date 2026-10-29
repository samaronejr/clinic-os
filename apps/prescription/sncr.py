"""Offline SNCR allocation contract from the September 2026 Anvisa manual.

These are documentation recordings, not allocations to Clinic OS. There is
no HTTP client, credential loader or production endpoint in this adapter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Literal, Protocol

from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.conf import settings

from apps.prescription.class_values import PrescriptionClass

if TYPE_CHECKING:
    from collections.abc import Mapping

type AllocationKind = Literal["NRA", "NRB", "NRB2", "NRR", "NRT", "RCE", "RET"]

CLASS_MODELS: dict[str, AllocationKind] = {
    PrescriptionClass.NOTIFICACAO_A: "NRA",
    PrescriptionClass.NOTIFICACAO_B: "NRB",
    PrescriptionClass.NOTIFICACAO_B2: "NRB2",
    PrescriptionClass.RETINOID: "NRR",
    PrescriptionClass.THALIDOMIDE: "NRT",
    PrescriptionClass.CONTROLE_ESPECIAL: "RCE",
    PrescriptionClass.ANTIMICROBIAL: "RET",
    PrescriptionClass.GLP1: "RET",
}
RECORDED_SNCR_PROVIDER = "sncr-documentation-v1"
MAX_RECORDING_BYTES = 16_384
RETAINED_BLOCK_SIZE = 1000
MIN_NOTIFICATION_QUANTITY = 10
MAX_NOTIFICATION_QUANTITY = 50
MAX_AVAILABLE_BALANCE = 2_147_483_647
INVALID_RECORDING = "sncr_invalid_recording"
NUMBERING_UNAVAILABLE = "sncr_numbering_unavailable"
RECONCILE = "sncr_reconcile"
NUMBER_PATTERN = r"[0-9]{4}\.[0-9]-[0-9]{2}\.[0-9]{7}"
JURISDICTIONS = frozenset(
    {
        "AC",
        "AL",
        "AP",
        "AM",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MT",
        "MS",
        "MG",
        "PA",
        "PB",
        "PR",
        "PE",
        "PI",
        "RJ",
        "RN",
        "RS",
        "RO",
        "RR",
        "SC",
        "SP",
        "SE",
        "TO",
    }
)


class SncrUnavailableError(ValueError):
    """Expose a stable refusal, never a request, number or provider response."""

    def __init__(self, code: str = "class_not_enabled") -> None:
        """Keep a machine-readable reason without reflecting provider data."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class AllocationRequest:
    """Exact prescriber/class request; fields come from a trusted profile."""

    category: str
    council: str
    jurisdiction: str
    registration: str
    quantity: int = 1
    cnpj: str = ""

    def payload(self) -> dict[str, str | int]:
        """Encode only the documented request fields for the selected class."""
        kind = CLASS_MODELS.get(self.category)
        if (
            kind is None
            or self.council not in {"CRM", "CRMV", "CRO"}
            or self.jurisdiction not in JURISDICTIONS
            or re.fullmatch(r"[0-9]{1,12}", self.registration) is None
            or type(self.quantity) is not int
            or not 1 <= self.quantity <= RETAINED_BLOCK_SIZE
        ):
            raise SncrUnavailableError(INVALID_RECORDING)
        common: dict[str, str | int] = {
            "conselho": self.council,
            "uf": self.jurisdiction,
            "documento": self.registration,
        }
        if kind in {"RCE", "RET"}:
            if self.quantity != 1 or re.fullmatch(r"[0-9]{14}", self.cnpj) is None:
                raise SncrUnavailableError(INVALID_RECORDING)
            return {**common, "tipo": kind, "cnpj": self.cnpj}
        if self.cnpj or not (
            MIN_NOTIFICATION_QUANTITY <= self.quantity <= MAX_NOTIFICATION_QUANTITY
        ):
            raise SncrUnavailableError(INVALID_RECORDING)
        return {**common, "receita": kind, "quantidade": self.quantity}

    @property
    def endpoint(self) -> str:
        """Return a relative documentation path, never a callable origin."""
        if CLASS_MODELS.get(self.category) in {"RCE", "RET"}:
            return "numeracoes/receita-especial-retencao"
        return "numeracoes/notificacao-receita"


@dataclass(frozen=True, slots=True, kw_only=True)
class AllocatedNumbers:
    """Retain exactly the returned block; never extend or manufacture numbers."""

    request: AllocationRequest
    first: str
    last: str
    quantity: int
    response: bytes


class SncrAdapter(Protocol):
    """Separate allocation from emission; neither implies legal issuance."""

    def allocate(self, request: AllocationRequest) -> AllocatedNumbers:
        """Resolve an existing prescriber allocation or refuse exhaustion."""
        ...

    def register_emission(self, *, document_digest: str) -> None:
        """Require a separately documented and approved registration contract."""
        ...


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SncrUnavailableError(INVALID_RECORDING)
        result[key] = value
    return result


class RecordedSncrAdapter:
    """Replay one documentation response for one exact prescriber request."""

    provider = RECORDED_SNCR_PROVIDER
    synthetic = True
    real_enabled = False

    def __init__(
        self,
        *,
        request: AllocationRequest,
        response: bytes | None,
        status: int = 200,
    ) -> None:
        """Supply a recording explicitly; the application has no default client."""
        self._request = request
        self._response = response
        self._status = status

    def allocate(self, request: AllocationRequest) -> AllocatedNumbers:
        """Validate exact request, response shape and prescriber before use."""
        if resolved_data_mode() != SYNTHETIC_DATA_MODE or not getattr(
            settings, "PRESCRIPTION_SYNTHETIC_ISSUANCE", False
        ):
            raise SncrUnavailableError
        payload = request.payload()
        if request != self._request:
            raise SncrUnavailableError(INVALID_RECORDING)
        if self._response is None:
            raise SncrUnavailableError(RECONCILE)
        if self._status == HTTPStatus.BAD_REQUEST:
            raise SncrUnavailableError(NUMBERING_UNAVAILABLE)
        if (
            self._status != (HTTPStatus.CREATED if "tipo" in payload else HTTPStatus.OK)
            or not 0 < len(self._response) <= MAX_RECORDING_BYTES
        ):
            raise SncrUnavailableError(INVALID_RECORDING)
        try:
            facts = json.loads(self._response, object_pairs_hook=_closed_object)
        except (ValueError, UnicodeError) as error:
            raise SncrUnavailableError(INVALID_RECORDING) from error
        if not isinstance(facts, dict):
            raise SncrUnavailableError(INVALID_RECORDING)
        return self._numbers(request, payload, facts, self._response)

    def _numbers(
        self,
        request: AllocationRequest,
        payload: Mapping[str, str | int],
        facts: dict[str, object],
        response: bytes,
    ) -> AllocatedNumbers:
        retained = "tipo" in payload
        expected = (
            {"inicio", "fim", "quantidade", "mensagem"}
            if retained
            else {"numeracoesReceita", "saldoReceitas", "mensagem"}
        )
        if facts.keys() != expected or not isinstance(facts["mensagem"], str):
            raise SncrUnavailableError(INVALID_RECORDING)
        if retained:
            first, last = facts["inicio"], facts["fim"]
            if (
                not isinstance(first, str)
                or not isinstance(last, str)
                or re.fullmatch(NUMBER_PATTERN, first) is None
                or re.fullmatch(NUMBER_PATTERN, last) is None
                or first[:-7] != last[:-7]
                or int(first[-7:]) < 1
                or int(last[-7:]) - int(first[-7:]) + 1 != RETAINED_BLOCK_SIZE
                or type(facts["quantidade"]) is not int
                or facts["quantidade"] != RETAINED_BLOCK_SIZE
            ):
                raise SncrUnavailableError(INVALID_RECORDING)
            quantity = RETAINED_BLOCK_SIZE
        else:
            numbers = facts["numeracoesReceita"]
            balance = facts["saldoReceitas"]
            if (
                not isinstance(numbers, str)
                or re.fullmatch(
                    r"\[" + NUMBER_PATTERN + r"(?:, " + NUMBER_PATTERN + r")*\]",
                    numbers,
                )
                is None
                or type(balance) is not int
                or not 0 <= balance <= MAX_AVAILABLE_BALANCE
            ):
                raise SncrUnavailableError(INVALID_RECORDING)
            returned = numbers[1:-1].split(", ")
            quantity = len(returned)
            if quantity > request.quantity or len(set(returned)) != quantity:
                raise SncrUnavailableError(INVALID_RECORDING)
            first, last = returned[0], returned[-1]
        return AllocatedNumbers(
            request=request,
            first=first,
            last=last,
            quantity=quantity,
            response=response,
        )

    def register_emission(self, *, document_digest: str) -> None:
        """Fail closed: the inspected manual specifies no emission endpoint."""
        if re.fullmatch(r"[0-9a-f]{64}", document_digest) is None:
            raise SncrUnavailableError(INVALID_RECORDING)
        raise SncrUnavailableError
