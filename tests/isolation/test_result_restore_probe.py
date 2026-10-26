"""Result source references extend, but never open, the recovery probe schema."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from ops.testing.restore_contract import RestoreContractError
from ops.testing.restore_rehearsal import _read_probe

if TYPE_CHECKING:
    from pathlib import Path


def test_result_source_probe_preserves_the_closed_recovery_binding(
    tmp_path: Path,
) -> None:
    organization = str(uuid4())
    payload = {
        "organization_id": organization,
        "kek": "0" * 64,
        "envelope_hex": "01",
        "expected_sha256": "0" * 64,
        "result_version_id": str(uuid4()),
        "clinical_document_id": str(uuid4()),
    }
    path = tmp_path / "probe.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    assert _read_probe(path).organization_id == organization
    payload["untrusted_field"] = "not-authority"
    path.write_text(json.dumps(payload))
    with pytest.raises(RestoreContractError):
        _read_probe(path)
