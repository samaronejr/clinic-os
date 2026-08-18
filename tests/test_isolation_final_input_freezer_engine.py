from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing import isolation_candidate_records as candidate_records
from ops.testing import isolation_common as c
from ops.testing import isolation_final_input_freezer as freezer
from ops.testing import isolation_terminal_publisher_authorizations as publisher_auth

from isolation_candidate_fixtures import corrupt_final_candidate, set_json_path
from isolation_final_input_freezer_fixtures import (
    freeze_fixture,
    is_json_value,
    only_claim,
)

if TYPE_CHECKING:
    from pathlib import Path

SHA, TREE = "a" * 40, "b" * 40
CREATED: Final = "2026-07-16T11:00:00.000000Z"
VERIFIED: Final = "2026-07-16T22:00:00.000000Z"


def test_final_input_freezer_rejects_missing_bound_inputs(tmp_path: Path) -> None:
    # Given: exact candidate evidence with its final probe removed.
    request = freeze_fixture(tmp_path)
    request.final_probe_path.unlink()

    # When / Then: input publication is absent when probe evidence is missing.
    with pytest.raises(c.IsolationError, match="probe"):
        _ = freezer.freeze_final_inputs(request)
    assert not request.output_path.exists()


def test_final_input_freezer_publishes_once_and_refuses_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: all receipts, both candidate pairs, both probes, and a clean source.
    request = freeze_fixture(tmp_path)

    original_publish = freezer.publish_manifest_bytes

    def interrupted_publish(destination: Path, raw: bytes, token: str) -> None:
        original_publish(destination, raw, token)
        message = "simulated post-publication interruption"
        raise RuntimeError(message)

    monkeypatch.setattr(freezer, "publish_manifest_bytes", interrupted_publish)
    with pytest.raises(RuntimeError, match="interruption"):
        _ = freezer.freeze_final_inputs(request)
    monkeypatch.setattr(freezer, "publish_manifest_bytes", original_publish)

    # When: replay adopts the published prefix and persists its observation.
    output = freezer.freeze_final_inputs(request)

    # Then: replay is byte-identical and authenticated drift fails closed.
    original = output.read_bytes()
    manifest = json.loads(original)
    bindings = manifest["runtime_bindings"]
    assert bindings["ledger_path"] == str(request.ledger_path)
    assert bindings["attempt_root"] == str(request.attempt_root)
    assert bindings["review_controller_root"] == str(
        request.attempt_root / "review-lane-controllers"
    )
    assert bindings["codex_launcher"]["resolved_sha256"] == c.raw_sha256(
        request.codex_source_path.read_bytes()
    )
    assert bindings["uv_launcher"]["resolved_sha256"] == c.raw_sha256(
        request.uv_source_path.read_bytes()
    )
    assert output.stat().st_mode & 0o777 == c.MODE_IMMUTABLE
    assert freezer.freeze_final_inputs(request).read_bytes() == original
    ledger, _ = c.load_json(request.ledger_path)
    claim = only_claim(ledger)
    _ = publisher_auth.validate_terminal_publisher_claim(claim, output.parent)
    observations = candidate_records.object_values(
        candidate_records.object_value(claim["observed"], "observed")[
            "published_outputs"
        ],
        "publisher observations",
    )
    assert observations[3]["status"] == "published"
    ledger_raw = c.canonical_bytes(ledger)
    dirty_source = replace(request.source_inspection, clean=False)
    dirty_request = replace(request, source_inspection=dirty_source)
    with pytest.raises(c.IsolationError, match="source"):
        _ = freezer.freeze_final_inputs(dirty_request)
    observations[3]["root_path"] = str(request.output_path.parent.parent)
    c.write_atomic_replace(request.ledger_path, c.canonical_bytes(ledger))
    with pytest.raises(c.IsolationError, match="publisher"):
        _ = freezer.freeze_final_inputs(request)
    c.write_atomic_replace(request.ledger_path, ledger_raw)
    request.runner_history_path.unlink()
    with pytest.raises(c.IsolationError, match="history"):
        _ = freezer.freeze_final_inputs(request)
    assert output.read_bytes() == original


def test_final_input_freezer_rejects_noncanonical_destination(tmp_path: Path) -> None:
    # Given: authenticated inputs but a caller-selected terminal filename.
    request = freeze_fixture(tmp_path)
    request = replace(request, output_path=request.output_path.with_name("alias.json"))

    # When / Then: publication fails before any destination appears.
    with pytest.raises(c.IsolationError, match="destination"):
        _ = freezer.freeze_final_inputs(request)
    assert not request.output_path.exists()


def test_final_input_freezer_rejects_unclaimed_staging_root(tmp_path: Path) -> None:
    # Given: valid evidence but a private staging sibling absent from the ledger.
    request = freeze_fixture(tmp_path)
    unclaimed = request.staging_root.with_name("88888888-8888-4888-8888-888888888888")
    unclaimed.mkdir(mode=c.MODE_DIRECTORY)
    request = replace(request, staging_root=unclaimed)
    ledger_raw = request.ledger_path.read_bytes()

    # When / Then: the unclaimed root cannot authorize terminal publication.
    with pytest.raises(c.IsolationError, match="publisher"):
        _ = freezer.freeze_final_inputs(request)
    assert not request.output_path.exists()
    assert request.ledger_path.read_bytes() == ledger_raw


@pytest.mark.parametrize(
    "defect",
    f"""unexpected=null claim_id="publisher" root_relative_path="claims/elsewhere"
    kind="process" dependency_claim_ids=["bad"] desired.owned_files=[{{}}]
    desired.unexpected=null desired.published_outputs=[]
    desired.published_outputs.0.root_path="/tmp"
    desired.published_outputs.1.predecessor_authorization_ids=[]
    desired.published_outputs.0.authorization_id="wrong"
    desired.published_outputs.0.mode=384 observed.unexpected=null
    observed.owned_files=[{{}}] observed.published_outputs=[]
    observed.published_outputs.0.root_path="/tmp" runner_creation={{}}
    candidate_envelope_binding={{}} activated_at_utc=null reserved_at_utc=null
    last_verified_at_utc="{CREATED}" claim-root=absent""".split(),
)
def test_final_input_freezer_requires_complete_terminal_publisher(
    tmp_path: Path, defect: str
) -> None:
    request = freeze_fixture(tmp_path)
    ledger, _ = c.load_json(request.ledger_path)
    claim = only_claim(ledger)
    if defect == "claim-root=absent":
        (request.attempt_root / str(claim["root_relative_path"])).rmdir()
    else:
        path, value = defect.split("=", 1)
        parsed: object = json.loads(value)
        assert is_json_value(parsed)
        set_json_path(claim, path, parsed)
    c.write_atomic_replace(request.ledger_path, c.canonical_bytes(ledger))

    with pytest.raises(c.IsolationError, match="publisher"):
        _ = freezer.freeze_final_inputs(request)
    assert not request.output_path.exists()


@pytest.mark.parametrize(
    "defect",
    f"""history.relative_path="alias.json" history.root_path="/tmp"
    history.predecessor_root_path="/tmp" history.predecessor_entries_sha256="{SHA}"
    envelope.schema_version=2 history.schema_version=2
    envelope.published_at_utc="not-a-time"
    envelope.published_at_utc="2026-07-16T10:00:00.000000Z"
    envelope.published_at_utc="2026-07-16T23:00:00.000000Z" claim=relabel""".split(),
)
def test_final_input_freezer_correlates_candidate_publication_records(
    tmp_path: Path, defect: str
) -> None:
    request = freeze_fixture(tmp_path)
    corrupt_final_candidate(
        request.application_envelope_path, request.application_history_path, defect
    )

    with pytest.raises(c.IsolationError, match="candidate"):
        _ = freezer.freeze_final_inputs(request)
    assert not request.output_path.exists()
