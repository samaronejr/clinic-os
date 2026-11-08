"""Deterministic local fiscal responses; these bytes have no fiscal validity."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar

from apps.billing.fiscal_policy import FiscalRefusal, FiscalRefusedError
from apps.billing.fiscal_provider import (
    AggregatorFiscalProvider,
    FiscalRequest,
    NationalFiscalProvider,
    decode_xml,
    encode_xml,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.billing.fiscal_provider import FiscalProvider


class SyntheticFiscalTransport:
    """No socket, URL, certificate, account, HTTP client or mutable external state."""

    recorded: ClassVar = True

    def __init__(self, *, request: FiscalRequest, aggregator: bool) -> None:
        """Bind every response to this opaque local document identity."""
        self.requested = request
        self.aggregator = aggregator

    def request(
        self, *, method: str, path: str, body: Mapping[str, str] | None
    ) -> bytes:
        """Produce only clearly marked rehearsal XML and its bounded envelope."""
        expected = self.requested
        reference = f"SYNTHETIC-NOT-FISCAL-{expected.reference.hex}"
        cancelling = path.endswith("/eventos") or path == "/documents/cancel"
        if method == "POST" and body is not None:
            field = (
                "xml_gzip_base64"
                if self.aggregator
                else "pedidoRegistroEventoXmlGZipB64"
                if cancelling
                else "dpsXmlGZipB64"
            )
            if body.get(field):
                decode_xml(body[field])
        marker = (
            "SYNTHETIC-NOT-FISCAL-CANCELLED" if cancelling else "SYNTHETIC-NOT-FISCAL"
        )
        xml = (
            f'<rehearsal reference="{expected.reference}" '
            f'dps="{expected.dps_id}">{marker}</rehearsal>'
        ).encode("ascii")
        if self.aggregator:
            payload: dict[str, object] = {
                "environment": "synthetic",
                "dps_id": expected.dps_id,
                "reference": reference,
                "status": "cancelled" if cancelling else "authorized",
                "xml_gzip_base64": encode_xml(xml),
            }
        else:
            payload = {
                "tipoAmbiente": 2,
                "idDPS": expected.dps_id,
                "chaveAcesso": reference,
            }
            if not path.startswith("/dps/"):
                field = "eventoXmlGZipB64" if cancelling else "nfseXmlGZipB64"
                payload[field] = encode_xml(xml)
        return json.dumps(payload).encode("ascii")


def rehearsal_provider(*, request: FiscalRequest, provider: str) -> FiscalProvider:
    """Select only the local fake; provider availability never enables live egress."""
    if provider not in {"national", "aggregator"}:
        raise FiscalRefusedError(FiscalRefusal.CONFIGURATION_INVALID)
    transport = SyntheticFiscalTransport(
        request=request, aggregator=provider == "aggregator"
    )
    if provider == "national":
        return NationalFiscalProvider(transport=transport)
    return AggregatorFiscalProvider(transport=transport)
