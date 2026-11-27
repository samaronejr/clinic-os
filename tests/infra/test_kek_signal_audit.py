"""AST census and negative controls for the KEK lane; fixture snippets never run."""

from __future__ import annotations

from pathlib import Path

import pytest
from ops.testing import kek_signal_audit as audit
from ops.testing.pidfd_command import Command, run_text


def test_entire_kek_lane_has_no_numeric_signal_fallback() -> None:
    root = Path(__file__).resolve().parents[2]
    sources = audit.scope(root)
    assert sources
    assert audit.audit(sources) == []


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("import os as api\nfire = api.kill\nfire(number, sig)", "numeric-signal"),
        ("from os import killpg as action\naction(group, sig)", "numeric-signal"),
        ("import signal\nsignal.pthread_kill(tid, sig)", "numeric-signal"),
        ("import subprocess as sp\nsp.Popen(argv).kill()", "numeric-signal"),
        ("job.terminate()", "numeric-signal"),
        ("method = getattr(job, 'send_signal')\nmethod(sig)", "numeric-signal"),
        (
            "from subprocess import run as cmd\ncmd(argv, timeout=1)",
            "implicit-timeout-signal",
        ),
        (
            "import subprocess as sp\nsp.check_output(argv, **options)",
            "implicit-timeout-signal",
        ),
        ("import subprocess\nsubprocess.run(['/bin/kill', arg])", "signal-command"),
    ],
    ids=[
        "kill",
        "killpg",
        "pthread",
        "popen-kill",
        "terminate",
        "send-signal",
        "timed-run",
        "opaque-kwargs",
        "kill-binary",
    ],
)
def test_lint_only_snippets_reject_numeric_paths(source: str, rule: str) -> None:
    findings = audit.python_findings("new_driver.py", source)
    assert findings
    assert {finding.rule for finding in findings} == {rule}


def test_census_discovers_renamed_and_untracked_harness_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(*args: str) -> str:
        return run_text(
            ("/usr/bin/git", *args), Command(5, cwd=tmp_path), check=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (tmp_path / "unchanged.py").write_text("pass\n")
    git("add", "--", "unchanged.py")
    git("commit", "-qm", "base")
    monkeypatch.setattr(audit, "BASE", git("rev-parse", "HEAD"))
    old = tmp_path / "first.py"
    old.write_text("pass\n")
    git("add", "--", old.name)
    git("commit", "-qm", "introduce unrelated name in lane")
    git("mv", old.name, "renamed.py")
    (tmp_path / "new-helper.sh").write_text('kill "$child"\n')
    (tmp_path / "new-driver").write_text(
        "#!/usr/bin/env python3\nimport os as system\nsystem.killpg(group, sig)\n"
    )
    (tmp_path / "renamed.py").write_text(
        "import subprocess as sp\nsp.run(cmd, timeout=1)\n"
    )
    sources = audit.scope(tmp_path)
    assert set(sources) == {"renamed.py", "new-helper.sh", "new-driver"}
    assert {f.rule for f in audit.audit(sources)} == {
        "implicit-timeout-signal",
        "numeric-signal",
        "signal-command",
    }
