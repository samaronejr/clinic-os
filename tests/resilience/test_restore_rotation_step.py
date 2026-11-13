"""Todo 72: the recovery exercise's source-side key rotation step, on real roles."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from apps.tenancy.envelope import EnvelopeError, decrypt, encrypt
from ops.testing import restore_fixture

from resilience.test_key_rotation import store_kek, tenant_scope

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import TenantGraph

pytestmark = pytest.mark.django_db(transaction=True)
PURPOSE = "resilience.rotation-step-probe"
PLAINTEXT = b"synthetic rotation step probe"


def test_the_rotation_step_rewraps_every_dek_before_replacing_the_kek(
    tenant_graph: TenantGraph,
    synthetic_secret_backend: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: the exercise's single synthetic tenant with one DEK and envelope.
    monkeypatch.setattr(restore_fixture, "ORGANIZATION_ID", tenant_graph.organization_a)
    monkeypatch.setattr(restore_fixture, "OWNER_ID", tenant_graph.user_a)
    secret = synthetic_secret_backend / "tenant-kek.secret"
    old = secret.read_text()
    with tenant_scope(tenant_graph.organization_a):
        envelope = encrypt(purpose=PURPOSE, plaintext=PLAINTEXT)
    # When: the exercise rotates DEK then KEK before taking its backup.
    restore_fixture.rotate()
    # Then: both DEK versions are rewrapped and only then is the KEK replaced.
    assert json.loads(capsys.readouterr().out) == {
        "dek_versions": 2,
        "rewrapped_versions": 2,
    }
    new = secret.read_text()
    assert new != old
    assert len(new) == 64
    assert secret.stat().st_mode & 0o777 == 0o600
    assert not (synthetic_secret_backend / "tenant-kek.secret.rotating").exists()
    with tenant_scope(tenant_graph.organization_a):
        assert decrypt(purpose=PURPOSE, envelope=envelope) == PLAINTEXT
    # A stale escrow holding the pre-rotation KEK can no longer open the tenant.
    store_kek(synthetic_secret_backend, old)
    with pytest.raises(EnvelopeError), tenant_scope(tenant_graph.organization_a):
        decrypt(purpose=PURPOSE, envelope=envelope)
