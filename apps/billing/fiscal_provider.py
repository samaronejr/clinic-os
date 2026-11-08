"""NFS-e wire boundary with offline transports only; no certificate or egress."""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import re
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Final, Never, Protocol
from xml.parsers import expat

from django.utils.translation import gettext

from apps.billing.fiscal_policy import FiscalRefusal, FiscalRefusedError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

MAX_XML: Final = 2 * 1024 * 1024
RESTRICTED_ENVIRONMENT: Final = 2


@dataclass(frozen=True, slots=True)
class FiscalRequest:
    """Exact signed bytes at the boundary; local rehearsals are explicitly unsigned."""

    reference: UUID
    dps_id: str = field(repr=False)
    signed_xml: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class FiscalResponse:
    """Opaque fiscal content never enters audit, error text or a dataclass repr."""

    status: str
    dps_id: str = field(repr=False)
    provider_reference: str = field(default="", repr=False)
    xml: bytes = field(default=b"", repr=False)
    pdf: bytes = field(default=b"", repr=False)
    rejection_code: str = ""


class FiscalTransport(Protocol):
    """A recorded transport, not a generic network client."""

    @property
    def recorded(self) -> bool:
        """Identify an offline transport without granting mutation authority."""
        ...

    def request(
        self, *, method: str, path: str, body: Mapping[str, str] | None
    ) -> bytes:
        """Replay exact bytes or raise TimeoutError for an ambiguous outcome."""
        ...


class FiscalProvider(Protocol):
    """National and future approved aggregators share one domain result contract."""

    @property
    def real_enabled(self) -> bool:
        """Every real leg remains closed in this source-only implementation."""
        ...

    def submit(self, *, request: FiscalRequest) -> FiscalResponse:
        """Submit exact DPS bytes; transport success is not authorization."""
        ...

    def lookup(self, *, request: FiscalRequest) -> FiscalResponse:
        """Reconcile by the stable DPS identity rather than submitting another DPS."""
        ...

    def cancel(
        self,
        *,
        request: FiscalRequest,
        provider_reference: str,
        signed_event: bytes,
    ) -> FiscalResponse:
        """Return a recorded cancellation event; callers enforce its deadline."""
        ...


def _invalid() -> FiscalRefusedError:
    return FiscalRefusedError(FiscalRefusal.PROVIDER_RESPONSE_INVALID)


def _xml_bytes(value: bytes) -> bytes:
    if not value or len(value) > MAX_XML:
        raise _invalid()

    def refuse_doctype(*_arguments: object) -> Never:
        raise _invalid()

    try:
        parser = expat.ParserCreate()
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.StartDoctypeDeclHandler = refuse_doctype
        is_final = True
        parser.Parse(value, is_final)
    except expat.ExpatError as error:
        raise _invalid() from error
    return value


def encode_xml(value: bytes) -> str:
    """Encode the exact bounded bytes without rendering, signing or modifying them."""
    return base64.b64encode(gzip.compress(_xml_bytes(value), mtime=0)).decode("ascii")


def decode_xml(value: object) -> bytes:
    """Bound encoded and inflated sizes and refuse entities and malformed XML."""
    if not isinstance(value, str) or len(value) > MAX_XML * 4:
        raise _invalid()
    try:
        compressed = base64.b64decode(value, validate=True)
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        result = decoder.decompress(compressed, MAX_XML + 1)
    except (ValueError, binascii.Error, zlib.error) as error:
        raise _invalid() from error
    if (
        not decoder.eof
        or decoder.unconsumed_tail
        or decoder.unused_data
        or len(result) > MAX_XML
    ):
        raise _invalid()
    return _xml_bytes(result)


def _identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None
    ):
        raise _invalid()
    return value


def _payload(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_XML * 4:
        raise _invalid()
    try:
        result: object = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise _invalid() from error
    if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
        raise _invalid()
    return result


def _rejection(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", value) is None
    ):
        raise _invalid()
    return value


def rejection_message(code: str) -> str:
    """Translate a fixed message, not arbitrary provider descriptions or PII."""
    _rejection(code)
    return gettext(
        "The fiscal provider rejected the document. Ask the fiscal reviewer "
        "to check the configuration."
    )


class NationalFiscalProvider:
    """Padrão Nacional taxpayer API envelope; only synthetic recorded bytes run."""

    real_enabled: ClassVar = False

    def __init__(self, *, transport: FiscalTransport) -> None:
        """Retain an offline transport; there is no endpoint or credential setting."""
        self.transport = transport

    def _request(
        self, *, method: str, path: str, body: Mapping[str, str] | None = None
    ) -> dict[str, object]:
        if self.transport.recorded is not True:
            raise FiscalRefusedError(FiscalRefusal.BLOCKED_ON_EG)
        result = _payload(self.transport.request(method=method, path=path, body=body))
        if (
            type(result.get("tipoAmbiente")) is not int
            or result["tipoAmbiente"] != RESTRICTED_ENVIRONMENT
        ):
            raise _invalid()
        return result

    def _result(
        self, payload: Mapping[str, object], request: FiscalRequest
    ) -> FiscalResponse:
        if payload.get("idDPS") != request.dps_id:
            raise _invalid()
        errors = payload.get("erros")
        if errors is not None:
            if (
                not isinstance(errors, list)
                or not errors
                or not isinstance(errors[0], dict)
            ):
                raise _invalid()
            return FiscalResponse(
                status="rejected",
                dps_id=request.dps_id,
                rejection_code=_rejection(errors[0].get("Codigo")),
            )
        reference = _identifier(payload.get("chaveAcesso"))
        xml = payload.get("nfseXmlGZipB64")
        return FiscalResponse(
            status="authorized" if xml is not None else "submitted",
            dps_id=request.dps_id,
            provider_reference=reference,
            xml=decode_xml(xml) if xml is not None else b"",
        )

    def submit(self, *, request: FiscalRequest) -> FiscalResponse:
        """POST /nfse with gzip/base64 DPS; receipt is not payment or fiscal proof."""
        _identifier(request.dps_id)
        result = self._request(
            method="POST",
            path="/nfse",
            body={"dpsXmlGZipB64": encode_xml(request.signed_xml)},
        )
        return self._result(result, request)

    def lookup(self, *, request: FiscalRequest) -> FiscalResponse:
        """Resolve DPS -> access key -> XML, preserving the same document identity."""
        dps = self._request(method="GET", path=f"/dps/{_identifier(request.dps_id)}")
        if dps.get("idDPS") != request.dps_id or dps.get("erros") is not None:
            raise _invalid()
        reference = _identifier(dps.get("chaveAcesso"))
        result = self._request(method="GET", path=f"/nfse/{reference}")
        if result.get("chaveAcesso") != reference:
            raise _invalid()
        return self._result(result, request)

    def cancel(
        self,
        *,
        request: FiscalRequest,
        provider_reference: str,
        signed_event: bytes,
    ) -> FiscalResponse:
        """Require returned cancellation event bytes for this access key and DPS."""
        reference = _identifier(provider_reference)
        result = self._request(
            method="POST",
            path=f"/nfse/{reference}/eventos",
            body={"pedidoRegistroEventoXmlGZipB64": encode_xml(signed_event)},
        )
        if (
            result.get("idDPS") != request.dps_id
            or result.get("chaveAcesso") != reference
        ):
            raise _invalid()
        return FiscalResponse(
            status="cancelled",
            dps_id=request.dps_id,
            provider_reference=reference,
            xml=decode_xml(result.get("eventoXmlGZipB64")),
        )


class AggregatorFiscalProvider:
    """Internal aggregator contract; no vendor or production mapping is invented."""

    real_enabled: ClassVar = False

    def __init__(self, *, transport: FiscalTransport) -> None:
        """Accept only the same offline boundary as the national adapter."""
        self.transport = transport

    def _request(
        self, *, action: str, request: FiscalRequest, xml: bytes = b""
    ) -> FiscalResponse:
        if self.transport.recorded is not True:
            raise FiscalRefusedError(FiscalRefusal.BLOCKED_ON_EG)
        payload = _payload(
            self.transport.request(
                method="POST",
                path=f"/documents/{action}",
                body={
                    "command": str(request.reference),
                    "dps_id": request.dps_id,
                    "xml_gzip_base64": encode_xml(xml) if xml else "",
                },
            )
        )
        if (
            payload.get("environment") != "synthetic"
            or payload.get("dps_id") != request.dps_id
            or payload.get("status")
            not in {"submitted", "authorized", "rejected", "cancelled"}
        ):
            raise _invalid()
        status = str(payload["status"])
        if status == "rejected":
            return FiscalResponse(
                status=status,
                dps_id=request.dps_id,
                rejection_code=_rejection(payload.get("rejection_code")),
            )
        return FiscalResponse(
            status=status,
            dps_id=request.dps_id,
            provider_reference=_identifier(payload.get("reference")),
            xml=decode_xml(payload.get("xml_gzip_base64"))
            if status in {"authorized", "cancelled"}
            else b"",
        )

    def submit(self, *, request: FiscalRequest) -> FiscalResponse:
        """Preserve the approved payload rather than selecting tax codes."""
        return self._request(action="submit", request=request, xml=request.signed_xml)

    def lookup(self, *, request: FiscalRequest) -> FiscalResponse:
        """Reconcile by the original command, never a newly generated invoice."""
        return self._request(action="lookup", request=request)

    def cancel(
        self,
        *,
        request: FiscalRequest,
        provider_reference: str,
        signed_event: bytes,
    ) -> FiscalResponse:
        """Return an event only when the aggregator binds the same authorization."""
        result = self._request(action="cancel", request=request, xml=signed_event)
        if (
            result.status != "cancelled"
            or result.provider_reference != provider_reference
        ):
            raise _invalid()
        return result
