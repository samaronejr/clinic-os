from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import pytest
from ops.testing import isolation_candidate_records as candidate_records
from ops.testing import isolation_common as c
from ops.testing import isolation_final_input_auth as auth
from ops.testing import isolation_final_input_freezer as freezer
from ops.testing import isolation_terminal_publisher_authorizations as publisher_auth

from isolation_candidate_fixtures import (
    complete_empty_lineage,
    corrupt_final_candidate,
    final_candidate_contract,
    final_input_fixture,
    set_json_path,
    write_final_candidate_pair,
)
from isolation_terminal_publisher_fixtures import (
    publisher_claim,
    stale_publisher_ledger,
    terminal_authorizations,
)

SHA, TREE = "a" * 40, "b" * 40
CREATED: Final = "2026-07-16T11:00:00.000000Z"
VERIFIED: Final = "2026-07-16T22:00:00.000000Z"


def test_final_input_freezer_rejects_missing_bound_inputs(tmp_path: Path) -> None:
    # Given: exact candidate evidence with its final probe removed.
    request = _freeze_fixture(tmp_path)
    request.final_probe_path.unlink()

    # When / Then: input publication is absent when probe evidence is missing.
    with pytest.raises(c.IsolationError, match="probe"):
        _ = freezer.freeze_final_inputs(request)
    assert not request.output_path.exists()


def test_final_input_freezer_publishes_once_and_refuses_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: all receipts, both candidate pairs, both probes, and a clean source.
    request = _freeze_fixture(tmp_path)

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
    assert output.stat().st_mode & 0o777 == c.MODE_IMMUTABLE
    assert freezer.freeze_final_inputs(request).read_bytes() == original
    ledger, _ = c.load_json(request.ledger_path)
    claim = _only_claim(ledger)
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
    request = _freeze_fixture(tmp_path)
    request = replace(request, output_path=request.output_path.with_name("alias.json"))

    # When / Then: publication fails before any destination appears.
    with pytest.raises(c.IsolationError, match="destination"):
        _ = freezer.freeze_final_inputs(request)
    assert not request.output_path.exists()


def test_final_input_freezer_rejects_unclaimed_staging_root(tmp_path: Path) -> None:
    # Given: valid evidence but a private staging sibling absent from the ledger.
    request = _freeze_fixture(tmp_path)
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
    request = _freeze_fixture(tmp_path)
    ledger, _ = c.load_json(request.ledger_path)
    claim = _only_claim(ledger)
    if defect == "claim-root=absent":
        (request.attempt_root / str(claim["root_relative_path"])).rmdir()
    else:
        path, value = defect.split("=", 1)
        set_json_path(claim, path, cast("c.JsonValue", json.loads(value)))
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
    request = _freeze_fixture(tmp_path)
    corrupt_final_candidate(
        request.application_envelope_path, request.application_history_path, defect
    )

    with pytest.raises(c.IsolationError, match="candidate"):
        _ = freezer.freeze_final_inputs(request)
    assert not request.output_path.exists()


def _freeze_fixture(root: Path) -> auth.FinalInputFreeze:
    ledger_path, _journal_path = stale_publisher_ledger(root, "reserved")
    ledger, _ = c.load_json(ledger_path)
    ledger["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    boot_observation = candidate_records.object_value(
        ledger["boot_observation"], "boot observation"
    )
    boot_observation["boot_id"] = ledger["boot_id"]
    attempt = Path(str(ledger["attempt_root"]))
    worktree = Path(str(ledger["worktree_realpath"]))
    terminal = attempt.parents[1] / "clinic-os-phase1a-final" / "terminal"
    (attempt / "claims").mkdir(mode=0o700)
    terminal.mkdir(parents=True, mode=0o700, exist_ok=True)
    files = _frozen_inputs(attempt, terminal, ledger_path, ledger)
    staging = attempt / str(_only_claim(ledger)["root_relative_path"])
    app = auth.CandidateInspection(
        "sha256:" + "d" * 64, final_candidate_contract("application")
    )
    runner = auth.CandidateInspection(
        "sha256:" + "e" * 64, final_candidate_contract("browser-runner")
    )
    return auth.FinalInputFreeze(
        *(worktree, SHA, TREE, str(ledger["attempt_id"]), attempt),
        *(files["plan"], files["proof"], files["ledger"]),
        *(files["baseline"], files["kickoff"], files["final-probe"]),
        *(files["receipts"], files["app-envelope"], files["app-history"]),
        *(files["runner-envelope"], files["runner-history"]),
        app,
        runner,
        staging,
        terminal / "inputs.json",
        auth.SourceInspection(SHA, TREE, clean=True),
    )


def _frozen_inputs(
    root: Path,
    terminal: Path,
    ledger_path: Path,
    record: c.JsonObject,
) -> dict[str, Path]:
    plan = candidate_records.object_value(record["approved_plan"], "approved plan")
    proof = candidate_records.object_value(
        record["execution_host_preflight"], "execution proof"
    )
    baseline = candidate_records.object_value(record["baseline"], "baseline")
    shared = candidate_records.object_value(
        baseline["shared_evidence_manifest"], "shared baseline"
    )
    paths = {
        "plan": Path(str(plan["path"])),
        "proof": Path(str(proof["path"])),
        "baseline": Path(str(shared["path"])),
    }
    attempt_id = str(record["attempt_id"])
    for fixture_name, key in (
        ("kickoff_probe", "kickoff"),
        ("final_probe", "final-probe"),
    ):
        paths[key] = root / f"{key}.json"
        probe = final_input_fixture(fixture_name)
        probe["attempt_id"] = attempt_id
        probe["creation_boot_id"] = record["boot_id"]
        probe["proof_sha256"] = c.raw_sha256(paths["proof"].read_bytes())
        probe["child_path"] = (
            f"{probe['parent_path']}/clinic-os-phase1a-probe-{attempt_id}-{probe['purpose']}"
        )
        c.write_no_replace(paths[key], c.canonical_bytes(probe), mode=c.MODE_IMMUTABLE)
    receipts = root / "todo-evidence"
    receipts.mkdir(exist_ok=True)
    for todo in range(1, 21):
        path = receipts / f"task-{todo}-clinic-os-phase-1a-staff-scheduling.json"
        c.write_no_replace(
            path,
            c.canonical_bytes({"schema_version": 1, "todo": todo}),
            mode=c.MODE_IMMUTABLE,
        )
    app = write_final_candidate_pair(root, "application", attempt_id)
    runner = write_final_candidate_pair(root, "runner", attempt_id)
    complete_empty_lineage(root, attempt_id)
    record["created_at_utc"] = CREATED
    record["last_verified_at_utc"] = VERIFIED
    desired: c.JsonObject = {
        "owned_files": [],
        "published_outputs": terminal_authorizations(terminal),
    }
    publisher = publisher_claim(desired, "active")
    _publish_launcher_prefix(publisher)
    record["claims"] = [publisher]
    (root / str(publisher["root_relative_path"])).mkdir(mode=0o700)
    c.write_atomic_replace(ledger_path, c.canonical_bytes(record))
    paths.update({"ledger": ledger_path, "receipts": receipts, **app, **runner})
    return paths


def _only_claim(ledger: c.JsonObject) -> c.JsonObject:
    claims = candidate_records.object_values(ledger["claims"], "claims")
    assert len(claims) == 1
    return claims[0]


def _publish_launcher_prefix(claim: c.JsonObject) -> None:
    desired = candidate_records.object_value(claim["desired"], "publisher desired")
    observed = candidate_records.object_value(claim["observed"], "publisher observed")
    authorizations = candidate_records.object_values(
        desired["published_outputs"], "publisher authorizations"
    )
    observations = candidate_records.object_values(
        observed["published_outputs"], "publisher observations"
    )
    for index, raw in enumerate(
        (b"/opt/clinic/launcher\n", b"{}\n", b"f" * 64 + b"\n")
    ):
        destination = candidate_records.authorization_destination(authorizations[index])
        c.write_no_replace(destination, raw, mode=c.MODE_IMMUTABLE)
        observations[index] = candidate_records.published_observation(
            authorizations[index], raw
        )
