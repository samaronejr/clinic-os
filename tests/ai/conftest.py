"""Keep each adapter experiment local to the test that registers it."""

import pytest
from apps.ai import adapters


@pytest.fixture(autouse=True)
def isolated_provider_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the startup registry even when a test directly registers a fake."""
    monkeypatch.setattr(adapters, "_PROVIDERS", adapters._PROVIDERS.copy())
