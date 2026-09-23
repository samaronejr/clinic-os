from __future__ import annotations

import stat
import tempfile
from typing import TYPE_CHECKING, Literal, assert_never
from uuid import UUID

import pytest
from ops.testing import (
    browser_runner_probe,
    ci_postgres_stack,
    https_stack_runtime,
    image_smoke_controller,
    tls_export,
    tls_materializer,
)
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
)
from ops.testing.tls_materializer import MaterializerLease

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path
    from types import ModuleType

type Caller = Literal[
    "postgres", "https", "browser", "materializer", "image", "ca-export"
]
MIGRATED_CALLERS: tuple[Caller, ...] = (
    "postgres",
    "https",
    "browser",
    "materializer",
    "image",
    "ca-export",
)
STAGING_PURPOSE: dict[Caller, str] = {
    "postgres": "postgres",
    "https": "https",
    "browser": "browser-probe",
    "materializer": "materializer",
    "image": "image",
    "ca-export": "ca-export",
}
CLAIM = UUID("11111111-1111-4111-8111-111111111111")
IMAGE = "sha256:" + "a" * 64


class ReservationCapturedError(Exception):
    pass


def _consume[T](lease: AbstractContextManager[T]) -> None:
    with lease:
        pytest.fail("reservation should stop before external resources")


def _operation(
    caller: Caller, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, Callable[[], None], str, str]:
    ledger = repository / ".omo/evidence/isolation-ledger-phase1a.json"
    spec: JsonObject = {"claim_id": str(CLAIM), "purpose": "synthetic-contract"}
    match caller:
        case "postgres":
            return (
                ci_postgres_stack,
                lambda: ci_postgres_stack._reserve(ledger, spec),
                "ci-postgres-spec.json",
                "synthetic-contract",
            )
        case "https":
            return (
                https_stack_runtime,
                lambda: https_stack_runtime._reserve(ledger, spec),
                "https-stack-spec.json",
                "synthetic-contract",
            )
        case "browser":
            return (
                browser_runner_probe,
                lambda: browser_runner_probe.probe_runner_image(repository, IMAGE, {}),
                "spec.json",
                "browser-runner-image-probe",
            )
        case "materializer":
            monkeypatch.setattr(tls_materializer, "_postgres_image_id", lambda: IMAGE)
            return (
                tls_materializer,
                lambda: _consume(tls_materializer.materializer_lease(repository)),
                "materializer-spec.json",
                "tls-materializer",
            )
        case "image":

            def build() -> None:
                _ = image_smoke_controller.build_candidate(repository, "a" * 40)

            return (
                image_smoke_controller,
                build,
                "spec.json",
                "candidate-application-publisher",
            )
        case "ca-export":

            def public_certificate(_arguments: tuple[str, ...]) -> str:
                return (
                    "-----BEGIN CERTIFICATE-----\n"
                    "synthetic\n-----END CERTIFICATE-----\n"
                )

            monkeypatch.setattr(
                tls_export,
                "run_docker_command",
                public_certificate,
            )
            materializer = MaterializerLease(
                str(CLAIM), "synthetic", IMAGE, ledger, "synthetic", {}
            )
            return (
                tls_export,
                lambda: _consume(tls_export.public_ca_export(materializer)),
                "spec.json",
                "tls-public-ca-export",
            )
        case unreachable:
            assert_never(unreachable)


def _attempt_ledger(tmp_path: Path, run_name: str) -> tuple[Path, Path]:
    attempt = tmp_path / run_name
    attempt.mkdir(mode=0o700)
    ledger = tmp_path / ".omo/evidence/isolation-ledger-phase1a.json"
    ledger.parent.mkdir(parents=True)
    _ = ledger.write_bytes(canonical_bytes({"attempt_root": str(attempt)}))
    return attempt, ledger


def _capture_staged_spec(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    run_root: Path,
    expected: tuple[str, str, str],
) -> None:
    filename, staging_purpose, claim_purpose = expected

    def capture(actual_ledger: Path, path: Path) -> None:
        assert actual_ledger == ledger
        assert path.name == filename
        assert path.parent.parent == run_root
        assert path.parent.name.startswith(f"{staging_purpose}-")
        assert stat.S_IMODE(run_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        value, raw = load_json(path)
        assert raw == canonical_bytes(value)
        assert value["claim_id"] == str(CLAIM)
        assert value["purpose"] == claim_purpose
        raise ReservationCapturedError

    monkeypatch.setattr(module, "reserve_claim", capture)


@pytest.mark.parametrize("caller", MIGRATED_CALLERS)
@pytest.mark.parametrize("run_name", ["one", "two"])
def test_migrated_caller_stages_under_private_run_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: Caller, run_name: str
) -> None:
    # Given: a real attempt root and a poisoned legacy tempfile boundary.
    attempt, ledger = _attempt_ledger(tmp_path, run_name)
    module, operation, filename, claim_purpose = _operation(
        caller, tmp_path, monkeypatch
    )
    run_root = attempt / "runtime"
    _capture_staged_spec(
        module,
        monkeypatch,
        ledger,
        run_root,
        (filename, STAGING_PURPOSE[caller], claim_purpose),
    )
    if caller not in {"postgres", "https"}:
        monkeypatch.setattr(module, "uuid4", lambda: CLAIM)

    def poison_tempfile(*_args: object, **_kwargs: object) -> None:
        pytest.fail("legacy tempfile staging is forbidden")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", poison_tempfile)
    # When: the caller reaches its real staged-file reservation boundary.
    with pytest.raises(ReservationCapturedError):
        operation()
    # Then: refusal removes only the allocated child; the run root remains.
    assert list(run_root.iterdir()) == []
    assert ledger.read_bytes() == canonical_bytes({"attempt_root": str(attempt)})


@pytest.mark.parametrize("caller", MIGRATED_CALLERS)
def test_migrated_caller_rejects_non_private_run_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: Caller
) -> None:
    # Given: the caller's run root exists with a non-private access mode.
    attempt, _ledger = _attempt_ledger(tmp_path, "attempt")
    run_root = attempt / "runtime"
    run_root.mkdir(mode=0o755)
    run_root.chmod(0o755)  # umask-explicit: the drifted mode must hold under 077
    module, operation, _filename, _claim_purpose = _operation(
        caller, tmp_path, monkeypatch
    )

    def refuse(_ledger: Path, _path: Path) -> None:
        pytest.fail("reservation ran against a non-private run root")

    monkeypatch.setattr(module, "reserve_claim", refuse)
    if caller not in {"postgres", "https"}:
        monkeypatch.setattr(module, "uuid4", lambda: CLAIM)
    # When / Then: the path policy fails closed and preserves the drifted mode.
    with pytest.raises(IsolationError):
        operation()
    assert stat.S_IMODE(run_root.stat().st_mode) == 0o755


@pytest.mark.parametrize("caller", MIGRATED_CALLERS)
def test_migrated_caller_preserves_stale_run_root_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: Caller
) -> None:
    # Given: a leftover foreign child inside the caller's private run root.
    attempt, ledger = _attempt_ledger(tmp_path, "attempt")
    run_root = attempt / "runtime"
    run_root.mkdir(mode=0o700)
    stale = run_root / "stale-interrupted-child"
    stale.mkdir(mode=0o700)
    _ = (stale / "sentinel").write_bytes(b"preserve")
    module, operation, filename, claim_purpose = _operation(
        caller, tmp_path, monkeypatch
    )
    _capture_staged_spec(
        module,
        monkeypatch,
        ledger,
        run_root,
        (filename, STAGING_PURPOSE[caller], claim_purpose),
    )
    if caller not in {"postgres", "https"}:
        monkeypatch.setattr(module, "uuid4", lambda: CLAIM)
    # When: the caller stages and abandons one fresh runtime child.
    with pytest.raises(ReservationCapturedError):
        operation()
    # Then: only the pre-existing sibling remains, byte-identical.
    assert [path.name for path in run_root.iterdir()] == ["stale-interrupted-child"]
    assert (stale / "sentinel").read_bytes() == b"preserve"


@pytest.mark.parametrize(
    "writer", [image_smoke_controller._immutable_json, tls_export._immutable_json]
)
@pytest.mark.parametrize("run_name", ["one", "two"])
def test_observation_writer_preserves_exact_input_bytes(
    tmp_path: Path, writer: Callable[[Path, JsonObject], Path], run_name: str
) -> None:
    # Given: a separate root and a synthetic second-stage observation record.
    root = tmp_path / run_name
    root.mkdir(mode=0o700)
    expected = b'{"owned_files":[],"published_outputs":[]}\n'
    # When: each unchanged caller publishes its immutable observed.json input.
    result = writer(
        root / "observed.json", {"owned_files": [], "published_outputs": []}
    )
    # Then: path, bytes, and mode agree exactly with the ledger input contract.
    assert result == root / "observed.json"
    assert result.read_bytes() == expected
    assert stat.S_IMODE(result.stat().st_mode) == 0o400
