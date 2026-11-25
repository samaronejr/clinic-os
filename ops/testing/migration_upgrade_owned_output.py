"""Publish private upgrade output and receipt its creation descriptor."""

import os
from io import TextIOWrapper
from pathlib import Path
from stat import S_ISREG


def receipt(path: Path, stream: TextIOWrapper) -> None:
    """Send only filesystem identity, never output contents, to the wrapper."""
    fd = os.environ.get("UPGRADE_OWNED_OUTPUT_FD")
    if fd is None:
        return
    stat = os.fstat(stream.fileno())
    nonce = os.environ["UPGRADE_OWNED_OUTPUT_NONCE"]
    record = f"{nonce}\t{stat.st_dev}:{stat.st_ino}\t{path}\n"
    os.write(int(fd), os.fsencode(record))


def open_owned(path: Path) -> TextIOWrapper:
    """Create the output without modifying or adopting an existing entry."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        message = f"private_output_exists: {path.name}"
        raise FileExistsError(message) from error
    stream = TextIOWrapper(os.fdopen(fd, "wb"), encoding="utf-8")
    try:
        receipt(path, stream)
    except BaseException:
        stream.close()
        raise
    return stream


def same_output(path: Path, stream: TextIOWrapper) -> bool:
    """Verify a retained descriptor still names the current output."""
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    created = os.fstat(stream.fileno())
    return S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (
        created.st_dev,
        created.st_ino,
    )
