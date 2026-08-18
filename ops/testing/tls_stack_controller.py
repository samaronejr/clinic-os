"""Probe the ledger-owned TLS materializer through its bounded lifecycle."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Never

from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.tls_materializer import materializer_lease

VOLUME_COUNT = 6


def main() -> None:
    """Dispatch the smoke form; standalone export requires a retained lease."""
    if sys.argv[1:] == ["smoke"]:
        smoke_materializer(Path.cwd())
        return
    _fail("invalid TLS controller invocation")


def smoke_materializer(repository: Path) -> None:
    """Attest exact mounts, versions, modes, owners, and reverse cleanup."""
    with materializer_lease(repository) as lease:
        mounts = json.loads(
            run_docker_command(
                ("inspect", "--format", "{{json .Mounts}}", lease.container_id)
            )
        )
        if not isinstance(mounts, list) or len(mounts) != VOLUME_COUNT:
            _fail("materializer mount contract failed")
        names = {item.get("Name") for item in mounts if isinstance(item, dict)}
        if names != set(lease.volume_names.values()):
            _fail("materializer volume identity contract failed")
        proof = run_docker_command(
            (
                "exec",
                lease.container_id,
                "stat",
                "-c",
                "%u:%g:%a:%n",
                "/clinic-pki/tls/ca.key",
                "/clinic-pki/tls/ca.crt",
                "/clinic-source/tls/pg_hba.conf",
                "/clinic-restore/tls/pg_hba.conf",
                "/clinic-web/tls/tls.key",
                "/clinic-trust/db-ca.pem",
            )
        )
        sys.stdout.write(proof)
        sys.stdout.write(
            run_docker_command(
                (
                    "exec",
                    lease.container_id,
                    "bash",
                    "-ceu",
                    "postgres --version; openssl version; "
                    "cmp /clinic-source/tls/pg_hba.conf "
                    "/clinic-restore/tls/pg_hba.conf; "
                    "sha256sum /clinic-source/tls/pg_hba.conf "
                    "/clinic-restore/tls/pg_hba.conf",
                )
            )
        )


def _fail(message: str) -> Never:
    raise _TlsControllerError(message)


class _TlsControllerError(RuntimeError):
    pass


if __name__ == "__main__":
    main()
