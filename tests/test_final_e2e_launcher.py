from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from ops.testing import f3_launcher_manifest
from ops.testing import final_e2e_controller as controller

ROOT = Path(__file__).resolve().parents[1]


async def _run_launcher(
    argv: tuple[str, ...], environment: dict[str, str]
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    return process.returncode or 0, stdout, stderr


def test_final_shell_is_validation_only_then_immediate_clean_exec() -> None:
    source = (ROOT / "ops/testing/final_e2e.sh").read_text(encoding="utf-8")

    for executable in (
        "/usr/bin/readlink",
        "/usr/bin/stat",
        "/usr/bin/sha256sum",
        "/usr/bin/env",
    ):
        assert executable in source
    for forbidden in (" trap ", "timeout", "uv run", "stage_argv", "run_child_stage"):
        assert forbidden not in source
    assert "exec /usr/bin/env -i" in source
    assert "HOME=/nonexistent" in source
    assert "PATH=/usr/bin:/bin" in source
    assert '"$launcher" -I -P -B "$supervisor"' in source


def test_launcher_sidecars_reject_drift_and_clean_poisoned_environment(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "ops/testing"
    scripts.mkdir(parents=True)
    shell = scripts / "final_e2e.sh"
    shutil.copyfile(ROOT / "ops/testing/final_e2e.sh", shell)
    shell.chmod(0o755)
    supervisor_script = scripts / "final_e2e_supervisor.py"
    supervisor_script.write_text(
        "import os,sys\n"
        "expected={'HOME':'/nonexistent','LANG':'C.UTF-8','LC_ALL':'C.UTF-8',"
        "'PATH':'/usr/bin:/bin','TZ':'UTC'}\n"
        "raise SystemExit(0 if os.environ==expected and sys.argv[1::2]=="
        "['--sha','--inputs','--terminal-evidence-dir'] else 3)\n",
        encoding="utf-8",
    )
    controller_script = scripts / "final_e2e_controller.py"
    controller_script.write_text("raise SystemExit(99)\n", encoding="utf-8")
    terminal = tmp_path / "terminal"
    terminal.mkdir(mode=0o700)
    inputs = terminal / "inputs.json"
    inputs.write_bytes(b'{"schema_version":1}\n')
    inputs.chmod(0o400)
    launcher_dir = tmp_path / ".venv" / "bin"
    launcher_dir.mkdir(parents=True)
    launcher = launcher_dir / "python"
    launcher.symlink_to(sys.executable)
    f3_launcher_manifest.freeze_launcher_sidecars(
        inputs=inputs,
        launcher=launcher,
        shell=shell,
        supervisor=supervisor_script,
        controller=controller_script,
    )
    head = controller._git_text(ROOT, "rev-parse", "HEAD")
    environment = {
        **os.environ,
        "PATH": "/poisoned",
        "PYTHONPATH": "/poisoned",
        "PYTHONHOME": "/poisoned",
        "UV_PROJECT_ENVIRONMENT": "/poisoned",
        "VIRTUAL_ENV": "/poisoned",
        "HTTPS_PROXY": "http://poisoned.invalid",
    }

    argv = (
        str(shell),
        "--sha",
        head,
        "--inputs",
        str(inputs),
        "--terminal-evidence-dir",
        str(terminal / "F3"),
    )
    returncode, _stdout, stderr = asyncio.run(_run_launcher(argv, environment))

    assert returncode == 0, stderr.decode()
    launcher_path = terminal / "F3-launcher.path"
    launcher_path.chmod(0o600)
    returncode, _stdout, _stderr = asyncio.run(_run_launcher(argv, environment))
    assert returncode == 2
