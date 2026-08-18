from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from django.conf import settings
from ops.testing.totp_test_driver import (
    TotpDriverError,
    parse_arguments,
    read_context,
)

from otp_test_support import create_totp_device, fixed_otp_time, runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
DRIVER: Final = ("-m", "ops.testing.totp_test_driver")
REJECTED: Final = 2


def _app_dsn() -> str:
    database = settings.DATABASES["default"]
    return (
        f"postgresql://clinic_app:clinic_app_password@{database['HOST'] or 'localhost'}"
        f":{database['PORT'] or 5432}/{database['NAME']}"
    )


def _context(
    graph: RbacGraph,
    user_id: UUID,
    mode: str,
    last_t: int = -1,
) -> bytes:
    return json.dumps(
        {
            "dsn": _app_dsn(),
            "last_t": last_t,
            "mode": mode,
            "tenant_id": str(graph.organization_a),
            "user_id": str(user_id),
        }
    ).encode()


def _invoke(mode: str, payload: bytes) -> tuple[int, bytes, bytes, bytes]:
    context_read, context_write = os.pipe()
    code_read, code_write = os.pipe()
    os.write(context_write, payload)
    os.close(context_write)
    process = subprocess.run(  # noqa: S603 - fixed interpreter, closed argv.
        [
            sys.executable,
            *DRIVER,
            "--mode",
            mode,
            "--context-fd",
            str(context_read),
            "--code-fd",
            str(code_write),
        ],
        check=False,
        capture_output=True,
        cwd=PROJECT_ROOT,
        pass_fds=(context_read, code_write),
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    os.close(context_read)
    os.close(code_write)
    with os.fdopen(code_read, "rb", closefd=True) as stream:
        emitted = stream.read()
    return process.returncode, emitted, process.stdout, process.stderr


def test_pending_enrollment_emits_exactly_six_bytes_and_no_diagnostics(
    rbac_graph: RbacGraph,
) -> None:
    with runtime_role():
        create_totp_device(rbac_graph.physician, confirmed=False)

    code, emitted, stdout, stderr = _invoke(
        "pending-enrollment",
        _context(rbac_graph, rbac_graph.physician, "pending-enrollment"),
    )

    assert code == 0
    assert len(emitted) == 6
    assert emitted.isdigit()
    assert stdout == b""
    assert stderr == b""


def test_confirmed_next_counter_requires_a_strictly_increasing_counter(
    rbac_graph: RbacGraph,
) -> None:
    with runtime_role():
        create_totp_device(rbac_graph.physician, confirmed=True)

    accepted, emitted, _, _ = _invoke(
        "confirmed-next-counter",
        _context(rbac_graph, rbac_graph.physician, "confirmed-next-counter"),
    )
    reused, replayed, _, stderr = _invoke(
        "confirmed-next-counter",
        _context(
            rbac_graph,
            rbac_graph.physician,
            "confirmed-next-counter",
            last_t=2**40,
        ),
    )

    assert accepted == 0
    assert len(emitted) == 6
    assert reused == REJECTED
    assert replayed == b""
    assert stderr == b""


def test_mode_mismatched_device_is_rejected(rbac_graph: RbacGraph) -> None:
    with runtime_role():
        create_totp_device(rbac_graph.physician, confirmed=True)

    code, emitted, stdout, stderr = _invoke(
        "pending-enrollment",
        _context(rbac_graph, rbac_graph.physician, "pending-enrollment"),
    )

    assert code == REJECTED
    assert emitted == b""
    assert stdout == b""
    assert stderr == b""


def test_multiple_matching_devices_are_rejected(rbac_graph: RbacGraph) -> None:
    with runtime_role():
        create_totp_device(rbac_graph.physician, confirmed=False)
        create_totp_device(rbac_graph.physician, confirmed=False)

    code, emitted, _, _ = _invoke(
        "pending-enrollment",
        _context(rbac_graph, rbac_graph.physician, "pending-enrollment"),
    )

    assert code == REJECTED
    assert emitted == b""


def test_missing_device_is_rejected(rbac_graph: RbacGraph) -> None:
    code, emitted, _, _ = _invoke(
        "pending-enrollment",
        _context(rbac_graph, rbac_graph.physician, "pending-enrollment"),
    )

    assert code == REJECTED
    assert emitted == b""


def test_foreign_user_context_cannot_reach_another_users_device(
    rbac_graph: RbacGraph,
) -> None:
    with runtime_role():
        create_totp_device(rbac_graph.physician, confirmed=False)

    code, emitted, stdout, stderr = _invoke(
        "pending-enrollment",
        _context(rbac_graph, rbac_graph.clinic_admin, "pending-enrollment"),
    )

    assert code == REJECTED
    assert emitted == b""
    assert stdout == b""
    assert stderr == b""


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--mode", "pending-enrollment"],
        ["--mode", "unknown", "--context-fd", "3", "--code-fd", "4"],
        ["--context-fd", "3", "--mode", "pending-enrollment", "--code-fd", "4"],
        ["--mode", "pending-enrollment", "--context-fd", "x", "--code-fd", "4"],
        ["--mode", "pending-enrollment", "--context-fd", "2", "--code-fd", "4"],
        ["--mode", "pending-enrollment", "--context-fd", "3", "--code-fd", "4", "-x"],
    ],
)
def test_invalid_argument_vectors_are_rejected(arguments: list[str]) -> None:
    with pytest.raises(TotpDriverError):
        parse_arguments(arguments)


def test_oversized_or_malformed_context_frames_are_rejected() -> None:
    for payload in (b"", b"{}", b"[]", b"{" + b"a" * 8192 + b"}"):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, payload)
        os.close(write_fd)
        with pytest.raises((TotpDriverError, ValueError)):
            read_context(read_fd, "pending-enrollment")


def test_driver_never_persists_state_between_invocations(
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    with runtime_role(), fixed_otp_time():
        create_totp_device(rbac_graph.physician, confirmed=False)
    before = sorted(item.name for item in tmp_path.iterdir())

    code, emitted, _, _ = _invoke(
        "pending-enrollment",
        _context(rbac_graph, rbac_graph.physician, "pending-enrollment"),
    )

    assert code == 0
    assert len(emitted) == 6
    assert sorted(item.name for item in tmp_path.iterdir()) == before
