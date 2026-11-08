"""Local rehearsal requires an explicit gate even with a valid fixture review."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

import pytest
from apps.billing import fiscal
from apps.billing.fiscal_policy import FiscalRefusedError
from apps.billing.fiscal_rehearsal import rehearsal_provider
from apps.billing.models import FiscalConfiguration, FiscalDocument

from billing.test_fiscal import NOW, request, review

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper


@pytest.fixture
def rehearsal_document(monkeypatch: pytest.MonkeyPatch) -> FiscalDocument:
    monkeypatch.setattr("apps.billing.fiscal.timezone.now", lambda: NOW)
    monkeypatch.setattr(fiscal, "resolved_data_mode", lambda: "synthetic")
    return FiscalDocument(
        rehearsal=True,
        configuration=FiscalConfiguration(**asdict(review())),
    )


def test_rehearsal_is_disabled_when_the_setting_is_absent(
    rehearsal_document: FiscalDocument,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    monkeypatch.delattr(settings, "BILLING_SYNTHETIC_NFSE", raising=False)
    with pytest.raises(FiscalRefusedError) as refused:
        fiscal._require_rehearsal(rehearsal_document)
    assert refused.value.code == "BLOCKED-ON-EG"


@pytest.mark.parametrize("enabled", [False, None, 1, "true"])
def test_truthy_or_disabled_settings_do_not_enable_rehearsal(
    rehearsal_document: FiscalDocument,
    settings: SettingsWrapper,
    enabled: object,
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = enabled
    with pytest.raises(FiscalRefusedError) as refused:
        fiscal._require_rehearsal(rehearsal_document)
    assert refused.value.code == "BLOCKED-ON-EG"


def test_only_explicit_synthetic_rehearsal_is_accepted(
    rehearsal_document: FiscalDocument, settings: SettingsWrapper
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    fiscal._require_rehearsal(rehearsal_document)


def test_a_synthetic_setting_cannot_open_live_mode(
    rehearsal_document: FiscalDocument,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    monkeypatch.setattr(fiscal, "resolved_data_mode", lambda: "live")
    with pytest.raises(FiscalRefusedError) as refused:
        fiscal._require_rehearsal(rehearsal_document)
    assert refused.value.code == "BLOCKED-ON-EG"


def test_an_external_review_is_not_a_rehearsal_document(
    rehearsal_document: FiscalDocument, settings: SettingsWrapper
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    rehearsal_document.rehearsal = False
    with pytest.raises(FiscalRefusedError) as refused:
        fiscal._require_rehearsal(rehearsal_document)
    assert refused.value.code == "BLOCKED-ON-EG"


@pytest.mark.parametrize("provider", ["national", "aggregator"])
def test_local_factory_preserves_identity_and_marks_all_artifacts(
    provider: str,
) -> None:
    prepared = request()
    adapter = rehearsal_provider(request=prepared, provider=provider)
    authorized = adapter.submit(request=prepared)
    recovered = adapter.lookup(request=prepared)
    cancelled = adapter.cancel(
        request=prepared,
        provider_reference=authorized.provider_reference,
        signed_event=b"<evento>SYNTHETIC-NOT-FISCAL-CANCELLED</evento>",
    )
    assert adapter.real_enabled is False
    assert authorized == recovered
    assert authorized.status == "authorized"
    assert cancelled.status == "cancelled"
    for outcome in (authorized, recovered, cancelled):
        assert outcome.dps_id == prepared.dps_id
        assert outcome.provider_reference == authorized.provider_reference
        assert b"SYNTHETIC-NOT-FISCAL" in outcome.xml


def test_local_factory_does_not_invent_an_unreviewed_provider() -> None:
    with pytest.raises(FiscalRefusedError) as refused:
        rehearsal_provider(request=request(), provider="unreviewed")
    assert refused.value.code == "fiscal_configuration_invalid"
