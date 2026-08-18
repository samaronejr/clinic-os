from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest
from ops.testing import f3_kill_domain, f3_recovery, image_source
from ops.testing import final_e2e_controller as controller
from ops.testing import final_e2e_supervisor as supervisor
from ops.testing.browser_supervisor_session import BrowserSupervisorSession

ROOT = Path(__file__).resolve().parents[1]
FINAL_SUITES = ROOT / "ops/testing/final-required-browser-suites.txt"
CI_SUITES = ROOT / "ops/testing/ci-required-browser-suites.txt"
STAGES: Final = (
    ("preflight-and-claims", 120, "child"),
    ("tls-materializer-source-db", 600, "child"),
    ("owner-release-migrations", 900, "child"),
    ("owner-bootstrap", 300, "child"),
    ("source-https-and-runner-start", 300, "in-process"),
    ("owner-enrollment-start-ack-helper-confirm", 300, "in-process"),
    ("three-owner-helpers-three-provisions-two-staff-enrollments", 900, "in-process"),
    ("browser-visual", 1800, "in-process"),
    ("audit-posture", 600, "child"),
    ("restore-rehearsal", 1800, "child"),
    ("restored-https-smoke-audit", 600, "child"),
    ("reverse-cleanup", 300, "child"),
)


@dataclass
class _RecordingEffects:
    session: object = field(default_factory=object)
    stage_calls: list[str] = field(default_factory=list)
    child_calls: list[str] = field(default_factory=list)
    in_process_session_ids: list[int] = field(default_factory=list)

    def child_stage(self, name: str, seconds: int, argv: tuple[str, ...]) -> None:
        assert seconds > 0
        assert argv == ("stage", name)
        self.stage_calls.append(name)
        self.child_calls.append(name)

    def start_source_session(self) -> object:
        self.stage_calls.append(STAGES[4][0])
        self.in_process_session_ids.append(id(self.session))
        return self.session

    def continue_source_session(self, name: str, session: object) -> None:
        assert session is self.session
        self.stage_calls.append(name)
        self.in_process_session_ids.append(id(session))


def test_final_and_ci_suite_selectors_are_distinct_closed_inputs() -> None:
    assert FINAL_SUITES.read_bytes() == (
        b"availability\npatient\nruntime-https\nscheduling\n"
    )
    assert CI_SUITES.read_bytes() == b"availability\npatient\nscheduling\n"

    sources = [
        ("100644", "0" * 40, source_path)
        for source_path in image_source.RUNNER_SUITE_PATHS.values()
    ]
    assert image_source._available_suites("browser-runner", sources) == [
        "availability",
        "patient",
        "runtime-https",
        "scheduling",
    ]


def test_stage_machine_is_exact_and_stages_five_through_eight_share_session() -> None:
    assert (
        tuple((item.name, item.seconds, item.execution) for item in controller.STAGES)
        == STAGES
    )
    effects = _RecordingEffects()

    session = controller.run_state_machine(effects)

    assert session is effects.session
    assert effects.stage_calls == [name for name, _, _ in STAGES]
    assert effects.in_process_session_ids == [id(effects.session)] * 4
    assert effects.child_calls == [
        name for name, _, execution in STAGES if execution == "child"
    ]


def test_final_browser_supervisor_session_has_the_exact_nonserializable_identity(
    tmp_path: Path,
) -> None:
    staging_claim = "33333333-3333-4333-8333-333333333333"
    staging = tmp_path / "claims" / staging_claim
    staging.mkdir(parents=True)
    left, right = socket.socketpair()
    try:
        session = BrowserSupervisorSession(
            claim_id="11111111-1111-4111-8111-111111111111",
            container_id="a" * 64,
            profile="container-https",
            origin="https://phase1a.qa.clinic-os.dev:8443",
            runner_pid=1234,
            control_socket=left,
            next_sequence=0,
            staging_claim_id=staging_claim,
            staging_root=staging,
            publisher_claim_id="44444444-4444-4444-8444-444444444444",
            publication_authorization_id="f3-artifacts",
        )

        assert set(session.__dataclass_fields__) == {
            "claim_id",
            "container_id",
            "profile",
            "origin",
            "runner_pid",
            "control_socket",
            "next_sequence",
            "staging_claim_id",
            "staging_root",
            "publisher_claim_id",
            "publication_authorization_id",
        }
        with pytest.raises(Exception, match="never serialized"):
            session.__getstate__()
    finally:
        left.close()
        right.close()


def test_in_process_stage_expires_as_typed_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = controller.StageSpec("source-https-and-runner-start", 1, "in-process")
    monkeypatch.setattr(controller, "STAGES", (stage,))

    with pytest.raises(controller.StageDeadline, match="source-https"):
        controller.run_in_process_stage(
            stage.name, stage.seconds, lambda: time.sleep(3)
        )


def _clean_repository(tmp_path: Path) -> tuple[Path, Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    controller._git(repository, "init", "--initial-branch=main")
    tracked = repository / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    controller._git(repository, "add", "tracked.txt")
    controller._git(
        repository,
        "-c",
        "user.name=Clinic Test",
        "-c",
        "user.email=clinic-test@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    return repository, tracked, controller._git_text(repository, "rev-parse", "HEAD")


def test_exact_sha_guard_accepts_a_matching_clean_tree(tmp_path: Path) -> None:
    repository, _tracked, head = _clean_repository(tmp_path)

    observed_tree = controller.require_exact_checkout(repository, head, repository)

    assert observed_tree == controller._git_text(repository, "rev-parse", "HEAD^{tree}")


def test_exact_sha_guard_rejects_a_dirty_tree(tmp_path: Path) -> None:
    repository, tracked, head = _clean_repository(tmp_path)
    tracked.write_text("dirty\n", encoding="utf-8")

    with pytest.raises(controller.StageContractError, match="exact-SHA"):
        controller.require_exact_checkout(repository, head, repository)


def test_f3_prepared_journal_has_exact_keys_and_rejects_state_drift(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "delegated"
    parent.mkdir()
    record = f3_recovery.prepared_record(
        attempt_id="11111111-1111-4111-8111-111111111111",
        sha="a" * 40,
        boot_id="22222222-2222-4222-8222-222222222222",
        inputs_sha256="b" * 64,
        launcher_manifest_sha256="c" * 64,
        supervisor_pid=123,
        supervisor_start_ticks=456,
        cgroup_parent=f3_kill_domain.path_identity(parent),
        now="2026-08-18T12:00:00.000000Z",
    )

    assert set(record) == set(f3_recovery.JOURNAL_KEYS)
    f3_recovery.validate_record(record)
    drifted = dict(record)
    drifted["state"] = "running"
    with pytest.raises(f3_recovery.F3RecoveryError, match="schema"):
        f3_recovery.validate_record(drifted)


def test_kill_domain_refuses_populated_or_replaced_paths(tmp_path: Path) -> None:
    domain = tmp_path / "clinic-os-phase1a-11111111-1111-4111-8111-111111111111-f3"
    domain.mkdir()
    (domain / "cgroup.events").write_text("populated 0\nfrozen 0\n", encoding="ascii")
    (domain / "cgroup.procs").write_text("", encoding="ascii")
    (domain / "cgroup.kill").write_text("", encoding="ascii")
    identity = f3_kill_domain.path_identity(domain)

    f3_kill_domain.require_empty_domain(domain, identity)
    (domain / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="ascii")
    with pytest.raises(f3_kill_domain.KillDomainError, match="populated"):
        f3_kill_domain.require_empty_domain(domain, identity)


def test_supervisor_sets_subreaper_before_any_fork_and_owns_reaping() -> None:
    calls: list[tuple[int, int]] = []

    def activate(option: int, value: int) -> int:
        calls.append((option, value))
        return 0

    supervisor.require_subreaper(activate)

    assert calls == [(supervisor.PR_SET_CHILD_SUBREAPER, 1)]
    source = Path(supervisor.__file__).read_text(encoding="utf-8")
    assert source.index("require_subreaper") < source.index("os.fork(")
    assert "os.waitpid(" in source
    assert "PR_SET_PDEATHSIG" in source
    assert "start_new_session" not in source


def test_f3_python_entrypoints_cannot_exit_successfully_without_a_driver() -> None:
    supervisor_source = Path(supervisor.__file__).read_text(encoding="utf-8")
    controller_source = Path(controller.__file__).read_text(encoding="utf-8")

    assert "raise SystemExit(main())" in supervisor_source
    assert "raise SystemExit(main())" in controller_source
    assert supervisor.main([]) == 2
    assert controller.main([]) == 2


def test_supervisor_child_argv_is_closed_to_controller_and_stage_forms() -> None:
    python = ROOT / ".venv/bin/python"
    script = ROOT / "ops/testing/final_e2e_controller.py"
    assert supervisor.controller_argv(python, script, 9) == (
        str(python),
        "-I",
        "-P",
        "-B",
        str(script),
        "--control-fd",
        "9",
    )
    assert supervisor.stage_argv(python, script, "restore-rehearsal")[-2:] == (
        "--stage",
        "restore-rehearsal",
    )
    with pytest.raises(supervisor.F3SupervisorError, match="stage"):
        supervisor.stage_argv(python, script, "browser-visual")
