"""The RNDS exchange boundary every interop transport implements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from apps.interop.rnds_transport import RndsAnswer


class InteropAdapter(Protocol):
    """Send one RNDS bundle: an access token, then one ``POST /Bundle``.

    Implementations classify the answer (status, RNDS identifier, EHR codes)
    and raise ``RndsTransportError`` when no answer arrived. They never log,
    persist or return the body, the token or the certificate.
    """

    def submit_bundle(self, *, body: bytes, requester_cns: str) -> RndsAnswer:
        """Submit the exact bundle bytes on behalf of the requester CNS."""
        ...
