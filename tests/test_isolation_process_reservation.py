from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ops.testing.isolation_common import IsolationError, load_json

from isolation_claim_fixtures import CLAIM_ID, claim_transitions, snapshot, write_spec
from isolation_process_fixtures import process_spec

if TYPE_CHECKING:
    from pathlib import Path


def test_single_process_reservation_records_closed_identity_contract(
    tmp_path: Path,
) -> None:
    # Given: an immutable single-process spec bound to a real launcher identity.
    ledger_path = snapshot(tmp_path)
    spec = process_spec(tmp_path, CLAIM_ID)

    # When: the stable-lock claim transition reserves the process authority.
    reserved = claim_transitions().reserve_claim(
        ledger_path,
        write_spec(tmp_path, spec),
    )

    # Then: desired identity is preserved and every live observation is empty.
    ledger, _ = load_json(ledger_path)
    claims = ledger["claims"]
    assert reserved == CLAIM_ID
    assert isinstance(claims, list)
    assert isinstance(claims[0], dict)
    assert claims[0]["kind"] == "process"
    assert claims[0]["desired"] == spec["desired"]
    assert claims[0]["observed"] == {
        "listener_socket_inode": None,
        "listeners": [],
        "members": [],
    }


def test_process_reservation_rejects_argv_hash_drift_without_mutation(
    tmp_path: Path,
) -> None:
    # Given: a closed process spec whose argv digest does not match its argv.
    ledger_path = snapshot(tmp_path)
    spec = process_spec(tmp_path, CLAIM_ID)
    desired = spec["desired"]
    assert isinstance(desired, dict)
    desired["argv_sha256"] = "f" * 64
    before = ledger_path.read_bytes()

    # When: reservation authenticates the NUL-delimited argv identity.
    with pytest.raises(IsolationError, match="argv"):
        claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))

    # Then: no process authority or mutable root is created.
    ledger, _ = load_json(ledger_path)
    assert ledger_path.read_bytes() == before
    assert ledger["claims"] == []


def test_gunicorn_process_reservation_requires_all_config_fields(
    tmp_path: Path,
) -> None:
    # Given: a process marked Gunicorn-prefork but retaining null config fields.
    ledger_path = snapshot(tmp_path)
    spec = process_spec(tmp_path, CLAIM_ID)
    desired = spec["desired"]
    assert isinstance(desired, dict)
    desired["process_model"] = "gunicorn-prefork"
    desired["module"] = "gunicorn"
    desired["worker_count"] = 2
    before = ledger_path.read_bytes()

    # When: reservation checks the conditional Gunicorn identity matrix.
    with pytest.raises(IsolationError, match="Gunicorn config"):
        claim_transitions().reserve_claim(ledger_path, write_spec(tmp_path, spec))

    # Then: the invalid partial Gunicorn authority leaves no state behind.
    assert ledger_path.read_bytes() == before
