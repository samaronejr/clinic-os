from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import rfc8785
from ops.testing import isolation_candidate_contract as candidate_contract
from ops.testing import isolation_candidate_records as candidate_records
from ops.testing import isolation_final_input_auth as final_input_auth
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    utc_now,
    write_atomic_replace,
)

from isolation_claim_fixtures import (
    CLAIM_ID,
    FOUNDATION_SHA,
    claim_transitions,
    write_immutable_json,
    write_spec,
)

if TYPE_CHECKING:
    from collections.abc import Callable

TREE_SHA: Final = "b" * 40
IMAGE_ID: Final = "sha256:" + ("c" * 64)
FINAL_APPLICATION_CLAIM: Final = "44444444-4444-4444-8444-444444444444"
FINAL_RUNNER_CLAIM: Final = "66666666-6666-4666-8666-666666666666"
FINAL_INPUT_TEMPLATE: Final = (
    Path(__file__).resolve().parents[1]
    / "fixtures/isolation/normative/final-input-freeze/valid.json"
)


def candidate_spec(tmp_path: Path, *, runner: bool = False) -> JsonObject:
    attempt_root = _attempt_root(tmp_path)
    desired = candidate_desired(attempt_root, CLAIM_ID, FOUNDATION_SHA, runner=runner)
    prefix = "candidate-browser-runner" if runner else "candidate-application"
    return {
        "claim_id": CLAIM_ID,
        "dependency_claim_ids": [],
        "desired": desired,
        "kind": "filesystem",
        "purpose": f"{prefix}-publisher",
    }


def candidate_desired(
    attempt_root: Path,
    claim_id: str,
    revision: str,
    *,
    runner: bool = False,
) -> JsonObject:
    prefix = "candidate-browser-runner" if runner else "candidate-application"
    name = "browser-runner-envelope.json" if runner else "application-envelope.json"
    base: JsonObject = {
        "gid": os.getegid(),
        "governing_lock": "stable",
        "mode": 0o400,
        "uid": os.geteuid(),
    }
    envelope: JsonObject = {
        **base,
        "authorization_id": f"{prefix}-envelope",
        "output_kind": "candidate-image",
        "predecessor_authorization_ids": [],
        "relative_paths": [f"{revision}/{name}"],
        "root_path": str(attempt_root / "candidate-images"),
    }
    history: JsonObject = {
        **base,
        "authorization_id": f"{prefix}-publication-history",
        "output_kind": "publication-history",
        "predecessor_authorization_ids": [f"{prefix}-envelope"],
        "relative_paths": [f"{claim_id}.json"],
        "root_path": str(attempt_root / "publication-history"),
    }
    return {"owned_files": [], "published_outputs": [envelope, history]}


def reserve_and_activate_candidate(tmp_path: Path, ledger_path: Path) -> JsonObject:
    spec = candidate_spec(tmp_path)
    transitions = claim_transitions()
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, spec))
    desired = _objects(spec["desired"])
    authorizations = _object_array(desired["published_outputs"])
    observed: JsonObject = {
        "owned_files": [],
        "published_outputs": [
            {
                "authorization_id": item["authorization_id"],
                "entries": [],
                "governing_lock": item["governing_lock"],
                "output_kind": item["output_kind"],
                "root_path": item["root_path"],
                "status": "unpublished",
            }
            for item in authorizations
        ],
    }
    path = write_immutable_json(tmp_path / "candidate-observed.json", observed)
    transitions.activate_claim(ledger_path, CLAIM_ID, path)
    return spec


def candidate_envelope(ledger: JsonObject) -> JsonObject:
    return {
        "attempt_id": ledger["attempt_id"],
        "authorization_id": "candidate-application-envelope",
        "claim_id": CLAIM_ID,
        "image_contract": {
            "available_suite_ids": [],
            "kind": "application",
            "revision_sha": FOUNDATION_SHA,
            "source_entry_count": 7,
            "source_manifest_sha256": "d" * 64,
            "tree_sha": TREE_SHA,
        },
        "image_id": IMAGE_ID,
        "published_at_utc": utc_now(),
        "revision_sha": FOUNDATION_SHA,
        "schema_version": 1,
        "tree_sha": TREE_SHA,
    }


def write_staged_envelope(claim_root: Path, envelope: JsonObject) -> Path:
    path = claim_root / "candidate-envelope.json"
    path.write_bytes(canonical_bytes(envelope))
    path.chmod(0o400)
    return path


def accepting_verifier() -> Callable[[JsonObject, JsonObject, JsonObject], None]:
    return lambda _ledger, _claim, _envelope: None


def complete_empty_lineage(attempt_root: Path, attempt_id: str) -> None:
    _seed, seed_raw = load_json(attempt_root / "receipt-lineage-seed.json")
    validation: JsonObject = {
        "current_attempt_id": attempt_id,
        "fix_sources": [],
        "next_fix_sequence": 1,
        "primary_sources": [],
        "schema_version": 1,
        "seed_sha256": raw_sha256(seed_raw),
    }
    validation_path = attempt_root / "receipt-lineage-validation.json"
    write_immutable_json(validation_path, validation)
    lineage = dict(validation)
    lineage["lineage_validation_sha256"] = raw_sha256(validation_path.read_bytes())
    write_immutable_json(attempt_root / "receipt-lineage.json", lineage)


def set_json_path(root: JsonObject, path: str, value: JsonValue) -> None:
    target: JsonValue = root
    for part in path.split(".")[:-1]:
        if isinstance(target, dict):
            target = target[part]
        else:
            assert isinstance(target, list)
            target = target[int(part)]
    assert isinstance(target, dict)
    target[path.rsplit(".", 1)[-1]] = value


def final_candidate_contract(kind: str) -> JsonObject:
    suites: list[JsonValue] = (
        [] if kind == "application" else list(final_input_auth.REQUIRED_SUITES)
    )
    return {
        "available_suite_ids": suites,
        "kind": kind,
        "revision_sha": FOUNDATION_SHA,
        "source_entry_count": 7,
        "source_manifest_sha256": "f" * 64,
        "tree_sha": TREE_SHA,
    }


def final_input_fixture(name: str) -> JsonObject:
    bundle, _raw = load_json(FINAL_INPUT_TEMPLATE)
    records = candidate_records.object_value(bundle["records"], "records")
    return candidate_records.object_value(records[name], name).copy()


def write_final_candidate_pair(
    root: Path, key: str, attempt_id: str
) -> dict[str, Path]:
    kind = "application" if key == "application" else "browser-runner"
    claim_id = FINAL_APPLICATION_CLAIM if kind == "application" else FINAL_RUNNER_CLAIM
    envelope = final_input_fixture(f"{key}_envelope")
    envelope["attempt_id"] = attempt_id
    envelope["image_contract"] = final_candidate_contract(kind)
    desired = candidate_desired(
        root, claim_id, FOUNDATION_SHA, runner=kind == "browser-runner"
    )
    claim: JsonObject = {
        "claim_id": claim_id,
        "desired": desired,
        "purpose": f"candidate-{kind}-publisher",
    }
    authorizations = candidate_records.object_values(
        desired["published_outputs"], "authorizations"
    )
    envelope_path = root / "candidate-images" / FOUNDATION_SHA / f"{kind}-envelope.json"
    envelope_path.parent.mkdir(parents=True, exist_ok=True)
    write_immutable_json(envelope_path, envelope)
    binding = candidate_contract.envelope_binding(envelope)
    observation = candidate_records.published_observation(
        authorizations[0], canonical_bytes(envelope)
    )
    history = candidate_records.candidate_history_record(
        {"attempt_id": attempt_id}, claim, binding, authorizations, observation
    )
    history_path = root / "publication-history" / f"{claim_id}.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    write_immutable_json(history_path, history)
    result_key = "app" if key == "application" else "runner"
    return {
        f"{result_key}-envelope": envelope_path,
        f"{result_key}-history": history_path,
    }


def corrupt_final_candidate(
    envelope_path: Path, history_path: Path, defect: str
) -> None:
    envelope, _ = load_json(envelope_path)
    history, _ = load_json(history_path)
    if defect == "claim=relabel":
        claim_id = "not-a-uuid"
        envelope["claim_id"] = claim_id
        history["claim_id"] = claim_id
        history["relative_path"] = f"{claim_id}.json"
    else:
        target, value = defect.split("=", 1)
        record, mutation_path = target.split(".", 1)
        set_json_path(
            envelope if record == "envelope" else history,
            mutation_path,
            json.loads(value),
        )
    binding = candidate_contract.envelope_binding(envelope)
    history["candidate_envelope_binding_sha256"] = raw_sha256(rfc8785.dumps(binding))
    for output_path, record_value in (
        (envelope_path, envelope),
        (history_path, history),
    ):
        write_atomic_replace(output_path, canonical_bytes(record_value))
        output_path.chmod(MODE_IMMUTABLE)


def _attempt_root(tmp_path: Path) -> Path:
    ledger_path = (
        tmp_path / "authority" / ".omo" / "evidence" / "isolation-ledger-phase1a.json"
    )
    ledger, _ = load_json(ledger_path)
    return Path(str(ledger["attempt_root"]))


def _objects(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _object_array(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return cast("list[JsonObject]", value)
