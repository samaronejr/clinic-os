"""Run the claimed production-image HTTPS acceptance lifecycle."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Never
from uuid import uuid4

from ops.testing.browser_runner_probe import probe_runner_image
from ops.testing.candidate_pair import validate_candidate_pair
from ops.testing.current_source_snapshot import verify_current_source_record
from ops.testing.https_stack_runtime import run_https_stack
from ops.testing.https_stack_specs import HttpsStackInput, build_https_stack_plan
from ops.testing.isolation_candidate_verification import verify_candidate_image
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
)
from ops.testing.process_helpers import run_process
from ops.testing.tls_export import public_ca_export
from ops.testing.tls_materializer import materializer_lease

if TYPE_CHECKING:
    from collections.abc import Callable

SHA_LENGTH = 40
CURRENT_SOURCE_ARGUMENTS = 5


def main() -> None:
    """Accept only the exact smoke forms validated by the shell wrappers."""
    repository = Path.cwd()
    browser_probe: Callable[[Path], None] | None = None
    if sys.argv[1:] == ["smoke"]:
        revision = _clean_revision(repository)
        ledger_path = repository / ".omo/evidence/isolation-ledger-phase1a.json"
        ledger, _ = load_json(ledger_path)
        envelope = _candidate_envelope(ledger, revision)
        image_id = _text(envelope["image_id"])
        image_contract = _object(envelope["image_contract"])
    elif (
        len(sys.argv[1:]) == CURRENT_SOURCE_ARGUMENTS
        and sys.argv[1] == "smoke-current-source"
        and sys.argv[2] == "--record"
        and sys.argv[4] == "--run-root"
    ):
        record = verify_current_source_record(
            repository,
            Path(sys.argv[3]),
            Path(sys.argv[5]),
        )
        image_id = _text(_object(record["images"])["application"])
        image_contract = _object(
            _object(_object(record["contexts"])["application"])["contract"]
        )
        browser_probe = _current_source_browser_probe(record)
    else:
        _fail("invalid HTTPS image controller invocation")
    _run_stack_phases(repository, image_id, image_contract, browser_probe=browser_probe)


def _current_source_browser_probe(
    record: JsonObject,
) -> Callable[[Path], None]:
    """Bind the verified record's browser-runner image to the probe seam."""
    image_id = _text(_object(record["images"]).get("browser-runner"))
    contract = _object(
        _object(_object(record["contexts"]).get("browser-runner")).get("contract")
    )

    def probe(repository: Path) -> None:
        probe_runner_image(repository, image_id, contract)

    return probe


def _run_stack_phases(
    repository: Path,
    image_id: str,
    image_contract: JsonObject,
    browser_probe: Callable[[Path], None] | None = None,
) -> None:
    with (
        materializer_lease(repository) as materializer,
        public_ca_export(materializer) as ca_export,
    ):
        identity = ca_export.path.stat(follow_symlinks=False)
        sys.stdout.write(
            "public-ca-export="
            f"uid:{identity.st_uid},gid:{identity.st_gid},"
            f"mode:{stat.S_IMODE(identity.st_mode):04o},sha256:{ca_export.sha256}\n"
        )
        for phase in ("source", "restore"):
            claim_id = str(uuid4())
            project = f"clinic_https_{phase}_{claim_id.split('-', 1)[0]}"
            plan = build_https_stack_plan(
                HttpsStackInput(
                    claim_id,
                    materializer.claim_id,
                    materializer.volume_names,
                    project,
                    materializer.image_id,
                    image_id,
                    image_contract,
                    ca_export.claim_id,
                    phase,
                )
            )
            run_https_stack(
                repository, plan, ca_export.path, browser_probe=browser_probe
            )
        sys.stdout.write(f"source-restore-application-image={image_id}\n")


def _clean_revision(repository: Path) -> str:
    status = run_process(
        ("/usr/bin/git", "-C", str(repository), "status", "--porcelain=v1")
    )
    revision = run_process(("/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"))
    if status.returncode != 0 or status.stdout or revision.returncode != 0:
        _fail("HTTPS image harness requires a clean current revision")
    value = revision.stdout.strip()
    if len(value) != SHA_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        _fail("HTTPS image harness revision is invalid")
    return value


def _candidate_envelope(ledger: JsonObject, revision: str) -> JsonObject:
    attempt_root = Path(_text(ledger["attempt_root"]))
    path = attempt_root / "candidate-images" / revision / "application-envelope.json"
    envelope, envelope_raw = load_json(path)
    claim_id = _text(envelope["claim_id"])
    _, history_raw = load_json(
        attempt_root / "publication-history" / f"{claim_id}.json"
    )
    claims = _objects(ledger["claims"])
    validated = validate_candidate_pair(envelope_raw, history_raw, claims)
    if validated.get("revision_sha") != revision:
        _fail("HTTPS candidate revision drifted")
    verify_candidate_image(ledger, {}, validated)
    return validated


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        _fail("HTTPS image object is invalid")
    return value


def _objects(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail("HTTPS image claims are invalid")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail("HTTPS image claims are invalid")
        result.append(item)
    return result


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        _fail("HTTPS image text is invalid")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    main()
