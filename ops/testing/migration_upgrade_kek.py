"""Write a synthetic tenant KEK into an explicitly supplied private directory."""

import os
import secrets
import sys
from pathlib import Path


def write_tenant_kek(run_directory: Path | None) -> None:
    """Create the key exclusively; never adopt a preexisting pathname."""
    if run_directory is None or not run_directory.is_absolute():
        message = "private_run_directory_required: tenant KEK"
        raise ValueError(message)
    directory = os.open(run_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = os.open(
            "tenant-kek.secret",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
    except FileExistsError as error:
        message = "private_output_exists: tenant-kek.secret"
        raise FileExistsError(message) from error
    finally:
        os.close(directory)
    with os.fdopen(fd, "w", encoding="ascii") as output:
        output.write(secrets.token_hex(32))


if __name__ == "__main__":
    arguments = sys.argv[1:]
    directory = Path(arguments[0]) if len(arguments) == 1 and arguments[0] else None
    write_tenant_kek(directory)
