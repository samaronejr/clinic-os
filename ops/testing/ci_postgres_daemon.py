"""Hold the dependency-linked TLS PostgreSQL claims for one hosted job."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Final, Never

from ops.testing.ci_postgres_controller import write_record
from ops.testing.ci_postgres_runtime import ci_database_lease, write_environment
from ops.testing.tls_export import public_ca_export
from ops.testing.tls_materializer import materializer_lease

STOP: Final = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
ARGUMENT_COUNT: Final = 2
PRIVATE_DIRECTORY_MODE: Final = 0o700
stop_state = {"requested": False}


def main() -> int:
    """Create claims before resources, attest readiness, and reverse-clean."""
    if len(sys.argv) != ARGUMENT_COUNT:
        _fail("invalid daemon invocation")
    root = Path(sys.argv[1])
    if (
        not root.is_absolute()
        or root.is_symlink()
        or root.stat().st_mode & 0o777 != PRIVATE_DIRECTORY_MODE
    ):
        _fail("invalid daemon root")
    for watched in STOP:
        signal.signal(watched, _stop)
    repository = Path.cwd()
    write_record(
        root / "supervisor.json",
        {"pgid": os.getpgrp(), "pid": os.getpid(), "start_ticks": _start()},
    )
    try:
        with (
            materializer_lease(repository) as materializer,
            public_ca_export(materializer) as public_ca,
            ci_database_lease(repository, materializer, public_ca) as database,
        ):
            write_environment(root / "ci-postgres.env", database)
            write_record(root / "ready", {}, mode=0o600)
            while not stop_state["requested"]:
                time.sleep(0.1)
    finally:
        write_record(root / "cleaned", {}, mode=0o600)
    return 0


def _stop(_signal_number: int, _frame: object) -> None:
    stop_state["requested"] = True


def _start() -> int:
    fields = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii").split()
    return int(fields[21])


def _fail(message: str) -> Never:
    raise RuntimeError(message)


if __name__ == "__main__":
    raise SystemExit(main())
