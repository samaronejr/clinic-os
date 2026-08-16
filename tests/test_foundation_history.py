from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
FOUNDATION_SHA: Final = "cffbb1900ae2132560f20c27fcf1a514a1ef71aa"


def test_foundation_history_accepts_the_exact_plan_command() -> None:
    # Given: the immutable foundation SHA and the committed guard entrypoint.
    command = PROJECT_ROOT / "ops" / "testing" / "assert_foundation_history.py"

    # When: the guard is invoked with the plan's exact positional grammar.
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script.
        (sys.executable, command, FOUNDATION_SHA),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: direct execution succeeds and emits the foundation-bound digest record.
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["foundation_sha"] == FOUNDATION_SHA
    assert record["migration_count"] > 0
