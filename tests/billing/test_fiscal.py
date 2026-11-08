"""Source-only fiscal policy and deliberately synthetic recorded API fixtures."""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.billing.fiscal_policy import (
    FiscalRefusedError,
    FiscalReview,
    require_fiscal_issuance,
    require_transition,
    validate_review,
)
from apps.billing.fiscal_provider import (
    AggregatorFiscalProvider,
    FiscalRequest,
    NationalFiscalProvider,
    decode_xml,
    encode_xml,
    rejection_message,
)
from django.utils.translation import gettext, override

if TYPE_CHECKING:
    from collections.abc import Mapping

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
REFERENCE = UUID("00000000-0000-4000-8000-000000000058")
DPS = "DPS-SYNTHETIC-000058"
FIXTURES = Path(__file__).parent / "fixtures" / "nfse"


def review() -> FiscalReview:
    return FiscalReview(
        legal_entity_reference=REFERENCE,
        municipality_code="0000000",
        service_code="SYNTHETIC-SERVICE",
        tax_regime="SYNTHETIC-REVIEWED",
        ibs_cbs_applicability="not_applicable",
        tax_fields={"ISS": {"classification": "SYNTHETIC-REVIEWED"}},
        review_reference=REFERENCE,
        reviewed_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        review_kind="synthetic_fixture",
        cancellation_window_seconds=3600,
    )


class FixtureTransport:
    """Offline response playback; no URL, socket, credential or provider exists."""

    recorded = True

    def __init__(self, *filenames: str) -> None:
        self.responses = [(FIXTURES / name).read_bytes() for name in filenames]
        self.calls: list[tuple[str, str, Mapping[str, str] | None]] = []

    def request(
        self, *, method: str, path: str, body: Mapping[str, str] | None
    ) -> bytes:
        self.calls.append((method, path, body))
        return self.responses.pop(0)


def request() -> FiscalRequest:
    return FiscalRequest(
        reference=REFERENCE,
        dps_id=DPS,
        signed_xml=b'<DPS Id="DPS-SYNTHETIC-000058">SYNTHETIC-NOT-FISCAL</DPS>',
    )


@pytest.mark.parametrize(
    "reviewed",
    [
        replace(review(), review_reference=None),
        replace(review(), reviewed_at=None),
        replace(review(), expires_at=NOW),
        replace(review(), reviewed_at=NOW + timedelta(seconds=1)),
        replace(review(), review_kind="eg9"),
    ],
)
def test_rehearsal_requires_current_explicit_fixture_review(
    reviewed: FiscalReview,
) -> None:
    with pytest.raises(FiscalRefusedError) as refused:
        validate_review(reviewed, now=NOW, rehearsal=True)
    assert refused.value.code == "fiscal_configuration_unreviewed"


@pytest.mark.parametrize(
    "reviewed",
    [
        replace(review(), municipality_code=""),
        replace(review(), service_code=""),
        replace(review(), tax_regime=""),
        replace(review(), ibs_cbs_applicability="undecided"),
        replace(review(), ibs_cbs_applicability="applicable"),
        replace(review(), tax_fields={"IBSCBS": {"cClassTrib": "SYNTHETIC"}}),
        replace(review(), cancellation_window_seconds=0),
    ],
)
def test_no_tax_or_deadline_defaults_are_inferred(reviewed: FiscalReview) -> None:
    with pytest.raises(FiscalRefusedError) as refused:
        validate_review(reviewed, now=NOW, rehearsal=True)
    assert refused.value.code == "fiscal_configuration_invalid"


def test_ibs_cbs_groups_are_preserved_only_when_explicitly_applicable() -> None:
    reviewed = replace(
        review(),
        ibs_cbs_applicability="applicable",
        tax_fields={
            "ISS": {"classification": "SYNTHETIC"},
            "IBSCBS": {"CST": "SYNTHETIC", "cClassTrib": "SYNTHETIC"},
        },
    )
    before = json.dumps(reviewed.tax_fields, sort_keys=True)
    validate_review(reviewed, now=NOW, rehearsal=True)
    assert json.dumps(reviewed.tax_fields, sort_keys=True) == before


def test_live_issuance_requires_review_before_reading_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def available(key: str, *, clinic_id: UUID) -> bool:
        calls.append(key)
        return True

    monkeypatch.setattr("apps.providers.services.is_live", available)
    with pytest.raises(FiscalRefusedError) as refused:
        require_fiscal_issuance(clinic_id=REFERENCE, review=review(), now=NOW)
    assert refused.value.code == "fiscal_configuration_unreviewed"
    assert not calls


def test_review_alone_never_bypasses_the_activated_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, UUID]] = []

    def unavailable(key: str, *, clinic_id: UUID) -> bool:
        calls.append((key, clinic_id))
        return False

    monkeypatch.setattr("apps.providers.services.is_live", unavailable)
    with pytest.raises(FiscalRefusedError) as refused:
        require_fiscal_issuance(
            clinic_id=REFERENCE, review=replace(review(), review_kind="eg9"), now=NOW
        )
    assert refused.value.code == "nfse_capability_unavailable"
    assert calls == [("nfse", REFERENCE)]


def test_even_an_asserted_live_capability_cannot_enable_the_unapproved_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.providers.services.is_live", lambda *a, **kw: True)
    with pytest.raises(FiscalRefusedError) as refused:
        require_fiscal_issuance(
            clinic_id=REFERENCE, review=replace(review(), review_kind="eg9"), now=NOW
        )
    assert refused.value.code == "BLOCKED-ON-EG"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("draft", "submitted"),
        ("draft", "cancelled"),
        ("submitted", "authorized"),
        ("submitted", "rejected"),
        ("authorized", "cancelled"),
    ],
)
def test_only_declared_fiscal_transitions_are_accepted(before: str, after: str) -> None:
    require_transition(
        status=before,
        next_status=after,
        now=NOW,
        cancel_until=NOW + timedelta(seconds=1),
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [("draft", "authorized"), ("rejected", "submitted"), ("cancelled", "authorized")],
)
def test_terminal_or_skipped_states_are_refused(before: str, after: str) -> None:
    with pytest.raises(FiscalRefusedError) as refused:
        require_transition(status=before, next_status=after, now=NOW, cancel_until=None)
    assert refused.value.code == "fiscal_transition_denied"


@pytest.mark.parametrize("cancel_until", [None, NOW, NOW - timedelta(seconds=1)])
def test_cancel_deadline_is_not_extended_or_guessed(
    cancel_until: datetime | None,
) -> None:
    with pytest.raises(FiscalRefusedError) as refused:
        require_transition(
            status="authorized",
            next_status="cancelled",
            now=NOW,
            cancel_until=cancel_until,
        )
    assert refused.value.code == "fiscal_cancel_deadline"


def test_national_authorization_preserves_bytes_and_correlates_dps() -> None:
    transport = FixtureTransport("authorized.json")
    provider = NationalFiscalProvider(transport=transport)
    result = provider.submit(request=request())
    method, path, body = transport.calls[0]
    assert (method, path) == ("POST", "/nfse")
    assert body is not None
    assert decode_xml(body["dpsXmlGZipB64"]) == request().signed_xml
    assert result.status == "authorized"
    assert result.dps_id == DPS
    assert result.xml == (FIXTURES / "authorized.xml").read_bytes()
    assert result.provider_reference == "SYNTHETIC-NOT-FISCAL-000058"
    assert provider.real_enabled is False
    assert result.pdf == b""


def test_lookup_recovers_by_dps_before_fetching_the_authorized_xml() -> None:
    transport = FixtureTransport("lookup.json", "authorized.json")
    result = NationalFiscalProvider(transport=transport).lookup(request=request())
    assert result.status == "authorized"
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", f"/dps/{DPS}"),
        ("GET", "/nfse/SYNTHETIC-NOT-FISCAL-000058"),
    ]


def test_rejection_keeps_only_bounded_code_not_provider_text_or_pii() -> None:
    result = NationalFiscalProvider(transport=FixtureTransport("rejected.json")).submit(
        request=request()
    )
    assert result.status == "rejected"
    assert result.rejection_code == "E0004"
    assert not result.xml
    with override("pt-br"):
        assert rejection_message(result.rejection_code) == gettext(
            "The fiscal provider rejected the document. Ask the fiscal reviewer "
            "to check the configuration."
        )
    assert "SYNTHETIC-PII-SENTINEL" not in repr(result)


def test_cancel_requires_a_recorded_event_not_a_successful_http_response() -> None:
    provider = NationalFiscalProvider(transport=FixtureTransport("cancelled.json"))
    result = provider.cancel(
        request=request(),
        provider_reference="SYNTHETIC-NOT-FISCAL-000058",
        signed_event=b"<evento>SYNTHETIC-CANCELLATION</evento>",
    )
    assert result.status == "cancelled"
    assert result.xml


@pytest.mark.parametrize("filename", ["wrong-dps.json", "production.json"])
def test_a_different_document_or_real_environment_cannot_become_authorized(
    filename: str,
) -> None:
    with pytest.raises(FiscalRefusedError) as refused:
        NationalFiscalProvider(transport=FixtureTransport(filename)).submit(
            request=request()
        )
    assert refused.value.code == "fiscal_provider_response_invalid"


@pytest.mark.parametrize(
    "encoded",
    [
        "not-base64",
        base64.b64encode(b"not-gzip").decode(),
        base64.b64encode(gzip.compress(b"x" * (2 * 1024 * 1024 + 1))).decode(),
        base64.b64encode(
            gzip.compress(b'<!DOCTYPE x [<!ENTITY phi "SYNTHETIC">]><x>&phi;</x>')
        ).decode(),
    ],
)
def test_xml_codec_refuses_invalid_or_unbounded_content(encoded: str) -> None:
    with pytest.raises(FiscalRefusedError) as refused:
        decode_xml(encoded)
    assert refused.value.code == "fiscal_provider_response_invalid"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-be", "utf-16-le"])
def test_xml_codec_refuses_doctype_regardless_of_xml_encoding(encoding: str) -> None:
    xml = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY phi "SYNTHETIC">]><x>&phi;</x>'
    ).encode(encoding)
    encoded = base64.b64encode(gzip.compress(xml)).decode("ascii")
    with pytest.raises(FiscalRefusedError) as refused:
        decode_xml(encoded)
    assert refused.value.code == "fiscal_provider_response_invalid"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-be", "utf-16-le"])
def test_xml_codec_preserves_valid_encoded_documents_exactly(encoding: str) -> None:
    xml = '<?xml version="1.0"?><x>SYNTHETIC-NOT-FISCAL</x>'.encode(encoding)
    assert decode_xml(encode_xml(xml)) == xml


def test_aggregator_has_the_same_contract_without_selecting_a_vendor() -> None:
    provider = AggregatorFiscalProvider(
        transport=FixtureTransport("aggregator-authorized.json")
    )
    result = provider.submit(request=request())
    assert result.status == "authorized"
    assert result.dps_id == DPS
    assert provider.real_enabled is False


def test_non_recorded_transport_is_refused_before_any_provider_call() -> None:
    transport = FixtureTransport("authorized.json")
    transport.recorded = False
    with pytest.raises(FiscalRefusedError) as refused:
        NationalFiscalProvider(transport=transport).submit(request=request())
    assert refused.value.code == "BLOCKED-ON-EG"
    assert not transport.calls


@pytest.mark.parametrize("raw", [b"{", b"[]", b"\xff"])
def test_malformed_recorded_envelopes_cannot_authorize_a_document(raw: bytes) -> None:
    transport = FixtureTransport("authorized.json")
    transport.responses[0] = raw
    with pytest.raises(FiscalRefusedError) as refused:
        NationalFiscalProvider(transport=transport).submit(request=request())
    assert refused.value.code == "fiscal_provider_response_invalid"
