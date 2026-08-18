from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Final

import pytest
from ops.testing.process_helpers import run_process

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GIT: Final = shutil.which("git")


def test_candidate_instruction_surfaces_refuse_before_invocation(
    tmp_path: Path,
) -> None:
    # Given: a committed candidate containing every forbidden instruction surface.
    module_path = PROJECT_ROOT / "ops/testing/render_review_input.py"
    assert module_path.is_file()
    specification = importlib.util.spec_from_file_location("review_input", module_path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    repository = tmp_path / "candidate"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "fixture@clinic-os.invalid")
    _git(repository, "config", "user.name", "Fixture")
    (repository / "AGENTS.md").write_text("invoke me\n", encoding="utf-8")
    nested = repository / "nested"
    nested.mkdir()
    (nested / "AGENTS.override.md").write_text("override\n", encoding="utf-8")
    config = repository / ".codex"
    config.mkdir()
    (config / "config.toml").write_text("model='attacker'\n", encoding="utf-8")
    rules = config / "rules"
    rules.mkdir()
    (rules / "malicious.rules").write_text("allow\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    sha = _git(repository, "rev-parse", "HEAD").strip()
    marker = tmp_path / "invoked"

    # When / Then: preflight rejects the tree and never calls the supplied tool.
    with pytest.raises(Exception, match="forbidden candidate instruction"):
        module.run_after_instruction_preflight(
            repository,
            sha,
            lambda: marker.write_text("invoked\n", encoding="utf-8"),
        )
    assert not marker.exists()


def _git(repository: Path, *arguments: str) -> str:
    assert GIT is not None
    result = run_process((GIT, "-C", str(repository), *arguments))
    assert result.returncode == 0, result.stderr
    return result.stdout
