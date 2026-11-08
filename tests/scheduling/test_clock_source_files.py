"""The source census reads current regular files, never symlink destinations."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from .clock_source_files import source_files

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("git_repository", [True, False])
def test_source_scan_skips_links_and_deleted_files_but_keeps_current_sources(
    tmp_path: Path, git_repository: bool
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    _ = (external / "outside.sql").write_text("external content")
    _ = (root / ".gitignore").write_text(".venv/\n")
    _ = (root / "tracked.sql").write_text("SELECT 1")
    removed = root / "removed.sql"
    _ = removed.write_text("SELECT 2")
    (root / ".venv").symlink_to(external, target_is_directory=True)
    (root / "linked.sql").symlink_to(external / "outside.sql")
    (root / "dangling.sql").symlink_to(external / "missing.sql")
    if git_repository:
        _ = subprocess.run(
            ("/usr/bin/git", "init", "--quiet"),
            cwd=root,
            check=True,
            capture_output=True,
        )
        _ = subprocess.run(
            (
                "/usr/bin/git",
                "add",
                "--",
                ".gitignore",
                "tracked.sql",
                "removed.sql",
                "linked.sql",
                "dangling.sql",
            ),
            cwd=root,
            check=True,
            capture_output=True,
        )
    removed.unlink()
    _ = (root / "current.arbitrary").write_text("SELECT 3")

    assert {
        str(path.relative_to(root)): source for path, source in source_files(root)
    } == {
        ".gitignore": ".venv/\n",
        "tracked.sql": "SELECT 1",
        "current.arbitrary": "SELECT 3",
    }
