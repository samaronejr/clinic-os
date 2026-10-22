"""Enumerate all tracked/current source files, not an extension vocabulary."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def source_files(root: Path) -> Iterator[tuple[Path, str]]:
    if (root / ".git").exists():
        result = subprocess.run(
            [
                "/usr/bin/git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )
        paths = [
            root / name.decode()
            for name in sorted(set(result.stdout.split(b"\0")))
            if name
        ]
    else:
        # Isolated guard fixtures have no Git index; every planted file is input.
        paths = sorted(path for path in root.rglob("*") if path.is_file())
    for path in paths:
        raw = path.read_bytes()
        if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            source = raw.decode("utf-32")
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            source = raw.decode("utf-16")
        elif b"\0" in raw:
            continue  # Git's binary discriminator, after recognizing text BOMs.
        else:
            source = raw.decode("utf-8-sig", errors="surrogateescape")
        yield path, source
