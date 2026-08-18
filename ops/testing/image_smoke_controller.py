"""Build, publish, and revalidate the candidate application image."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Never
from uuid import uuid4

from ops.testing.browser_runner_contract import selected_suites
from ops.testing.candidate_pair import validate_candidate_pair
from ops.testing.image_builder import build_candidate_image
from ops.testing.image_source import assemble_candidate_context
from ops.testing.isolation_candidate_contract import candidate_desired
from ops.testing.isolation_candidate_publication import publish_candidate_envelope
from ops.testing.isolation_candidate_verification import verify_candidate_image
from ops.testing.isolation_claim_transitions import (
    activate_claim,
    release_claim,
    reserve_claim,
)
from ops.testing.isolation_common import (
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    utc_now,
)

CONTROLLER_ARGUMENT_COUNT = 4


def main() -> None:
    """Dispatch the two closed controller forms from the shell grammar."""
    if len(sys.argv) != CONTROLLER_ARGUMENT_COUNT or sys.argv[2] != "--sha":
        _fail("invalid controller invocation")
    operation, revision = sys.argv[1], sys.argv[3]
    if operation == "build":
        build_candidate(Path.cwd(), revision)
    elif operation == "smoke":
        smoke_candidate(Path.cwd(), revision)
    else:
        _fail("invalid controller invocation")


def build_candidate(
    repository: Path,
    revision: str,
    kind: str = "application",
    required_suites: list[str] | None = None,
) -> str:
    """Build from one claim-owned Git-object context and publish its pair."""
    ledger_path = (repository / ".omo/evidence/isolation-ledger-phase1a.json").resolve(
        strict=True
    )
    ledger, _ = load_json(ledger_path)
    attempt_root = Path(_text(ledger.get("attempt_root")))
    if kind not in {"application", "browser-runner"}:
        _fail("candidate image kind is invalid")
    existing = attempt_root / "candidate-images" / revision / f"{kind}-envelope.json"
    if existing.exists():
        return smoke_candidate(repository, revision, kind, required_suites)
    claim_id = str(uuid4())
    placeholder: JsonObject = {
        "image_contract": {"kind": kind},
        "revision_sha": revision,
    }
    prefix = f"candidate-{kind}"
    spec: JsonObject = {
        "claim_id": claim_id,
        "dependency_claim_ids": [],
        "desired": candidate_desired(attempt_root, claim_id, placeholder),
        "kind": "filesystem",
        "purpose": f"{prefix}-publisher",
    }
    with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
        staging = Path(temporary)
        spec_path = _immutable_json(staging / "spec.json", spec)
        reserve_claim(ledger_path, spec_path)
        claim_root = attempt_root / "claims" / claim_id
        observed = _candidate_observation(spec)
        observed_path = _immutable_json(staging / "observed.json", observed)
        activate_claim(ledger_path, claim_id, observed_path)
        try:
            context = claim_root / "build-context"
            contract = assemble_candidate_context(
                repository,
                context,
                revision,
                kind,
            )
            selected_suites(
                _strings(contract.get("available_suite_ids")),
                [] if required_suites is None else required_suites,
            )
            image_id_path = claim_root / "image-id"
            build_candidate_image(context, contract, image_id_path)
            image_id = image_id_path.read_text(encoding="ascii").strip()
            current, _ = load_json(ledger_path)
            envelope: JsonObject = {
                "attempt_id": current["attempt_id"],
                "authorization_id": f"{prefix}-envelope",
                "claim_id": claim_id,
                "image_contract": contract,
                "image_id": image_id,
                "published_at_utc": utc_now(),
                "revision_sha": revision,
                "schema_version": 1,
                "tree_sha": contract["tree_sha"],
            }
            envelope_path = _immutable_json(
                claim_root / "candidate-envelope.json",
                envelope,
            )
            publish_candidate_envelope(ledger_path, claim_id, envelope_path)
        finally:
            if claim_root.exists() and not claim_root.is_symlink():
                shutil.rmtree(claim_root)
            release_claim(ledger_path, claim_id)
    return smoke_candidate(repository, revision, kind, required_suites)


def smoke_candidate(
    repository: Path,
    revision: str,
    kind: str = "application",
    required_suites: list[str] | None = None,
) -> str:
    """Reconstruct the pair, prove claim absence, and freshly inspect the image."""
    ledger_path = (repository / ".omo/evidence/isolation-ledger-phase1a.json").resolve(
        strict=True
    )
    ledger, _ = load_json(ledger_path)
    attempt_root = Path(_text(ledger.get("attempt_root")))
    envelope_path = (
        attempt_root / "candidate-images" / revision / f"{kind}-envelope.json"
    )
    envelope, envelope_raw = load_json(envelope_path)
    claim_id = _text(envelope.get("claim_id"))
    history_path = attempt_root / "publication-history" / f"{claim_id}.json"
    _, history_raw = load_json(history_path)
    claims = _objects(ledger.get("claims"))
    validated = validate_candidate_pair(envelope_raw, history_raw, claims)
    image_contract = _object(validated.get("image_contract"))
    selected_suites(
        _strings(image_contract.get("available_suite_ids")),
        [] if required_suites is None else required_suites,
    )
    verify_candidate_image(ledger, {}, validated)
    image_id = _text(validated.get("image_id"))
    sys.stdout.write(f"{image_id}\n")
    return image_id


def _candidate_observation(spec: JsonObject) -> JsonObject:
    desired = _object(spec.get("desired"))
    outputs = _objects(desired.get("published_outputs"))
    return {
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
            for item in outputs
        ],
    }


def _immutable_json(path: Path, value: JsonObject) -> Path:
    path.write_bytes(canonical_bytes(value))
    path.chmod(0o400)
    return path


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail("candidate controller object is invalid")
    return value


def _objects(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail("candidate controller array is invalid")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail("candidate controller array is invalid")
        result.append(item)
    return result


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        _fail("candidate controller text is invalid")
    return value


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        _fail("candidate controller string array is invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail("candidate controller string array is invalid")
        result.append(item)
    return result


def _fail(message: str) -> Never:
    raise _CandidateControllerError(message)


class _CandidateControllerError(RuntimeError):
    pass


if __name__ == "__main__":
    main()
