"""Run the rehearsal wrapper, then act as a peer on the host side mid-run.

Run inside ``unshare --user --map-root-user --mount --propagation shared`` so a
mount made here is an event the wrapper's own namespaces could receive. The
plan (a JSON file) names the command, its environment, the directory to mount
over, and a FIFO. A shim running inside the sandboxed child prints ``MOUNT_NOW``
and blocks on the FIFO; this helper then acts, and releases the child. No step
waits on a timer.

The action is a mount (the default), a file left visible to the child, the
same-user namespace escape, or a tamper: list the seal files a peer could
rewrite, rewrite the hash a forging child announced, and edit the launcher and
census in the checkout, which are restored when the run ends.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MOUNT = shutil.which("mount") or "/usr/bin/mount"
MARKER = "MOUNT_NOW"


def _tamper(
    tamper: dict[str, object],
    originals: dict[str, bytes],
    forged: tuple[str, str] | None,
) -> list[str]:
    """Edit the named checkout files; rewrite seals a peer could reach."""
    canary = repr(str(tamper["canary"]))
    for name in tamper["edit"]:  # type: ignore[attr-defined]
        original = Path(name).read_bytes()
        originals[name] = original
        Path(name).write_bytes(original + f"\nopen({canary}, 'w').close()\n".encode())
    directory = Path(str(tamper["runner_temp"]))
    seals = sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.name.startswith("migration-upgrade-seal")
    )
    if forged is not None:
        for seal in seals:
            manifest = json.loads((directory / seal).read_text(encoding="utf-8"))
            manifest[forged[0]][4] = forged[1]
            (directory / seal).write_text(json.dumps(manifest), encoding="utf-8")
    return seals


def main(plan_path: str) -> None:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    shared = Path(plan["mount_dir"])
    subprocess.run([MOUNT, "--bind", str(shared), str(shared)], check=True)  # noqa: S603 - fixed
    subprocess.run([MOUNT, "--make-shared", str(shared)], check=True)  # noqa: S603 - fixed
    # The child opens the release FIFO read-only; this end stays open for writing.
    release = os.open(plan["fifo"], os.O_RDWR)
    originals: dict[str, bytes] = {}
    seals: list[str] = []
    forged: tuple[str, str] | None = None
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
    try:
        for line in process.stdout:
            output.append(line)
            if line.startswith("FORGED "):
                _, name, digest = line.split()
                forged = (name, digest)
            if line.strip() == MARKER:
                late = shared / "late"
                if plan.get("escape"):
                    # The same-user attack: reach into the sandbox's namespaces and
                    # clear the read-only flag on the bind that exposes the target.
                    peer = Path(__file__).with_name("sandbox_userns_escape_helper.py")
                    attack = subprocess.run(  # noqa: S603 - fixed helper acting as the peer
                        [sys.executable, str(peer), str(process.pid), str(shared)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    output.append(f"ESCAPE {attack.stdout.strip()}\n")
                elif plan.get("tamper"):
                    seals = _tamper(plan["tamper"], originals, forged)
                elif plan.get("visible"):
                    # A host file the child can see, read-only, and must not change.
                    late.mkdir()
                    (late / "target").write_text(plan["secret"], encoding="utf-8")
                else:
                    late.mkdir()
                    mount = [MOUNT, "-t", "tmpfs", "tmpfs", str(late)]
                    subprocess.run(mount, check=True)  # noqa: S603 - fixed
                    (late / "target").write_text(plan["secret"], encoding="utf-8")
                    (late / "python3").mkdir()
                os.write(release, b"go\n")
        returncode = process.wait()
    finally:
        for name, original in originals.items():
            Path(name).write_bytes(original)
    target = shared / "late" / "target"
    entries = shared / "late" / "python3"
    canary = Path(plan["tamper"]["canary"]) if plan.get("tamper") else None
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
                "seals": seals,
                "canary": canary.exists() if canary is not None else None,
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1])
