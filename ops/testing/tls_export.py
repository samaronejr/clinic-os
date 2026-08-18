"""Claim and verify the executor-owned public CA export."""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ops.testing.isolation_claim_transitions import (
    activate_claim,
    release_claim,
    reserve_claim,
)
from ops.testing.isolation_common import JsonObject, canonical_bytes, load_json
from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.isolation_filesystem_claim import (
    load_current_filesystem_observation,
)
from ops.testing.isolation_refresh import verify_claim

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ops.testing.tls_materializer import MaterializerLease


@dataclass(frozen=True, slots=True)
class PublicCaExport:
    """Identity of one active executor-owned public certificate export."""

    claim_id: str
    path: Path
    sha256: str


@contextmanager
def public_ca_export(
    materializer: MaterializerLease,
) -> Iterator[PublicCaExport]:
    """Reserve before exporting and reverse-release the public-only CA file."""
    raw = run_docker_command(
        ("exec", materializer.container_id, "cat", "/clinic-trust/db-ca.pem")
    ).encode()
    if b"PRIVATE KEY" in raw or b"BEGIN CERTIFICATE" not in raw:
        raise _PublicCaExportError
    ledger, _ = load_json(materializer.ledger_path)
    attempt_root = Path(_text(ledger.get("attempt_root")))
    claim_id = str(uuid4())
    digest = hashlib.sha256(raw).hexdigest()
    desired: JsonObject = {
        "owned_files": [
            {
                "gid": os.getegid(),
                "mode": 0o600,
                "relative_path": "db-ca.pem",
                "sha256": digest,
                "uid": os.geteuid(),
            }
        ],
        "published_outputs": [],
    }
    spec: JsonObject = {
        "claim_id": claim_id,
        "dependency_claim_ids": [materializer.claim_id],
        "desired": desired,
        "kind": "filesystem",
        "purpose": "tls-public-ca-export",
    }
    with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
        staging = Path(temporary)
        spec_path = _immutable_json(staging / "spec.json", spec)
        reserve_claim(materializer.ledger_path, spec_path)
        claim_root = attempt_root / "claims" / claim_id
        export_path = claim_root / "db-ca.pem"
        with export_path.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        export_path.chmod(0o600)
        observed = load_current_filesystem_observation(spec, claim_root)
        observed_path = _immutable_json(staging / "observed.json", observed)
        activate_claim(materializer.ledger_path, claim_id, observed_path)
        try:
            yield PublicCaExport(claim_id, export_path, digest)
        finally:
            verify_claim(materializer.ledger_path, claim_id, refresh=True)
            export_path.unlink()
            claim_root.rmdir()
            release_claim(materializer.ledger_path, claim_id)


def _immutable_json(path: Path, value: JsonObject) -> Path:
    path.write_bytes(canonical_bytes(value))
    path.chmod(0o400)
    return path


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _PublicCaExportError
    return value


class _PublicCaExportError(RuntimeError):
    pass
