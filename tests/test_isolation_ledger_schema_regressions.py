from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURE_ROOT: Final = (
    PROJECT_ROOT / "tests" / "fixtures" / "isolation" / "normative" / "ledger-claims"
)


def test_null_runner_creation_accepts_a_reserved_filesystem_claim(
    tmp_path: Path,
) -> None:
    # Given: a schema-v2 ledger with one ordinary reserved filesystem publisher.
    ledger = json.loads((FIXTURE_ROOT / "ledger-envelope.json").read_text())
    ledger["claims"] = [
        {
            "activated_at_utc": None,
            "candidate_envelope_binding": None,
            "claim_id": "33333333-3333-4333-8333-333333333333",
            "dependency_claim_ids": [],
            "desired": {"owned_files": [], "published_outputs": []},
            "kind": "filesystem",
            "last_verified_at_utc": "2026-01-01T00:00:01.500000Z",
            "observed": {"owned_files": [], "published_outputs": []},
            "prepared_at_utc": None,
            "purpose": "todo-evidence-staging",
            "reserved_at_utc": "2026-01-01T00:00:01.000000Z",
            "root_relative_path": "claims/33333333-3333-4333-8333-333333333333",
            "runner_creation": None,
            "status": "reserved",
        }
    ]

    instance = tmp_path / "ledger.json"
    instance.write_text(json.dumps(ledger))
    validator = shutil.which("jsonschema")
    assert validator is not None

    # When: Draft 2020-12 evaluates property-only runner state conditionals.
    result = subprocess.run(  # noqa: S603 - fixed validator and test-owned inputs.
        [
            validator,
            "-V",
            "Draft202012Validator",
            str(PROJECT_ROOT / "ops" / "testing" / "isolation-ledger.schema.json"),
            "-i",
            str(instance),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: null does not vacuously enter intent or prepared runner branches.
    assert result.returncode == 0, result.stderr
