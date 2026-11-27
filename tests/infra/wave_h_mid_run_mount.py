"""Run the rehearsal wrapper, then mount something on the host side mid-run.

Run inside ``unshare --user --map-root-user --mount --propagation shared`` so a
mount made here is an event the wrapper's own namespaces could receive. The
plan (a JSON file) names the command, its environment, the directory to mount
over, and a FIFO. A shim running inside the sandboxed child prints ``MOUNT_NOW``
and blocks on the FIFO; this helper mounts a tmpfs, writes a secret into it,
then releases the child. No step waits on a timer.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MOUNT = shutil.which("mount") or "/usr/bin/mount"
MARKER = "MOUNT_NOW"


def main(plan_path: str) -> None:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    shared = Path(plan["mount_dir"])
    subprocess.run([MOUNT, "--bind", str(shared), str(shared)], check=True)  # noqa: S603 - fixed
    subprocess.run([MOUNT, "--make-shared", str(shared)], check=True)  # noqa: S603 - fixed
    # The child opens the release FIFO read-only; this end stays open for writing.
    release = os.open(plan["fifo"], os.O_RDWR)
    process = subprocess.Popen(  # noqa: S603 - the wrapper under test
        plan["argv"],
        env=plan["env"],
        cwd=plan["cwd"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    output = []
    for line in process.stdout:
        output.append(line)
        if line.strip() == MARKER:
            late = shared / "late"
            if plan.get("escape"):
                # The same-user attack: reach into the sandbox's namespaces and
                # clear the read-only flag on the bind that exposes the target.
                attack = subprocess.run(  # noqa: S603 - fixed helper acting as the peer
                    [
                        sys.executable,
                        str(
                            Path(__file__).with_name("sandbox_userns_escape_helper.py")
                        ),
                        str(process.pid),
                        str(shared),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                output.append(f"ESCAPE {attack.stdout.strip()}\\n")
            else:
                late.mkdir()
                subprocess.run([MOUNT, "-t", "tmpfs", "tmpfs", str(late)], check=True)  # noqa: S603 - fixed
                (late / "target").write_text(plan["secret"], encoding="utf-8")
                (late / "python3").mkdir()
            os.write(release, b"go\n")
    returncode = process.wait()
    target = shared / "late" / "target"
    entries = shared / "late" / "python3"
    sys.stdout.write(
        json.dumps(
            {
                "returncode": returncode,
                "output": "".join(output),
                "target": target.read_text(encoding="utf-8")
                if target.exists()
                else None,
                "python3": sorted(entry.name for entry in entries.iterdir())
                if entries.exists()
                else None,
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1])
