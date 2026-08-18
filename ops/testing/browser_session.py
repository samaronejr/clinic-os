"""Runtime entrypoint and confinement attestation for the browser image."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import shutil
import signal
import sys
import time
import zoneinfo
from importlib.metadata import distribution, version
from pathlib import Path, PurePosixPath
from typing import Final, Never

from ops.testing.process_helpers import run_process

RUNNER_ID: Final = 10001
TMPFS_TARGET: Final = PurePosixPath("/", "tmp", "clinic-browser").as_posix()
RUNNER_ROOT: Final = PurePosixPath("/", "runner").as_posix()
SHM_ROOT: Final = PurePosixPath("/", "dev", "shm").as_posix()


def attest_runtime() -> dict[str, object]:
    """Causally prove identity, dependencies, timezone, and write confinement."""
    if os.getuid() != RUNNER_ID or os.getgid() != RUNNER_ID:
        _fail()
    if os.environ.get("PYTHONTZPATH") != "" or zoneinfo.TZPATH != ():
        _fail()
    if version("tzdata") != "2026.3" or version("playwright") != "1.61.0":
        _fail()
    certutil = shutil.which("certutil")
    if certutil is None:
        _fail()
    certutil_version = run_process(
        (certutil, "-H"),
        timeout_seconds=5,
    )
    if certutil_version.returncode not in {0, 1, 255}:
        _fail()
    _probe_write_confinement()
    manifest_raw, manifest_entry_count = _manifest()
    return {
        "architecture": platform.machine(),
        "certutil": True,
        "chromium_revision": _chromium_revision(),
        "gid": os.getgid(),
        "manifest_entry_count": manifest_entry_count,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "playwright_version": version("playwright"),
        "schema_version": 1,
        "tmpfs_target": TMPFS_TARGET,
        "tzdata_version": version("tzdata"),
        "uid": os.getuid(),
        "zoneinfo_tzpath": list(zoneinfo.TZPATH),
    }


def _probe_write_confinement() -> None:
    writable = Path(TMPFS_TARGET) / "runtime-write-probe"
    writable.write_text("synthetic", encoding="ascii")
    writable.unlink()
    denied_paths = (
        Path(RUNNER_ROOT) / "runtime-write-probe",
        Path(SHM_ROOT) / "probe",
    )
    for denied in denied_paths:
        try:
            denied.write_text("synthetic", encoding="ascii")
        except OSError as error:
            if error.errno not in {errno.ENOENT, errno.EACCES, errno.EROFS}:
                raise
        else:
            denied.unlink(missing_ok=True)
            _fail()
    mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    if mountinfo.count(f" {TMPFS_TARGET} ") != 1:
        _fail()


def _manifest() -> tuple[bytes, int]:
    manifest_path = Path(RUNNER_ROOT) / "browser-runner-source-manifest.json"
    manifest_raw = manifest_path.read_bytes()
    manifest: object = json.loads(manifest_raw)
    if not isinstance(manifest, dict):
        _fail()
    entries: object = manifest.get("entries")
    if not isinstance(entries, list):
        _fail()
    return manifest_raw, len(entries)


def _chromium_revision() -> str:
    package = distribution("playwright")
    files = package.files
    if files is None:
        _fail()
    matches = [
        item
        for item in files
        if item.as_posix().endswith("driver/package/browsers.json")
    ]
    if len(matches) != 1:
        _fail()
    document_path = Path(str(package.locate_file(matches[0])))
    document: object = json.loads(document_path.read_bytes())
    if not isinstance(document, dict):
        _fail()
    browsers: object = document.get("browsers")
    if not isinstance(browsers, list):
        _fail()
    chromium = [
        item
        for item in browsers
        if isinstance(item, dict) and item.get("name") == "chromium"
    ]
    if len(chromium) != 1:
        _fail()
    revision: object = chromium[0].get("revision")
    if not isinstance(revision, str):
        _fail()
    if not (Path("/ms-playwright") / f"chromium-{revision}").is_dir():
        _fail()
    return revision


def main() -> None:
    """Serve the inert probe lifecycle or emit one canonical attestation."""
    if sys.argv[1:] == ["attest"]:
        sys.stdout.write(json.dumps(attest_runtime(), sort_keys=True) + "\n")
        return
    if sys.argv[1:] != ["hold"]:
        _fail()
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    while True:
        time.sleep(3600)


def _fail() -> Never:
    raise _BrowserRuntimeError


class _BrowserRuntimeError(RuntimeError):
    pass


if __name__ == "__main__":
    main()
