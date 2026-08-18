"""Execute the four final-wave controller forms through durable barriers."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.final_wave_state import (
    record_f4_created,
    record_final_frozen,
    record_inputs_frozen,
    record_pre_f4_frozen,
)
from ops.testing.isolation_barrier_process import barrier_argv_sha256
from ops.testing.isolation_common import (
    IsolationError,
    load_json,
    raw_sha256,
    utc_now,
)
from ops.testing.isolation_controller_kernel import RecoveryPolicy
from ops.testing.isolation_final_staging import (
    activate_final_staging,
    ensure_final_control_root,
    release_final_staging,
)
from ops.testing.isolation_final_wave_child import (
    _boot_id,
    _process_is_live,
    _run_final_child,
    _start_ticks,
)
from ops.testing.isolation_final_wave_commands import (
    final_predecessor,
    final_wave_environment,
    final_wave_payload,
    scope_claim_id,
)
from ops.testing.isolation_final_wave_controller import acquire_final_wave
from ops.testing.isolation_final_wave_failure import publish_final_wave_failure
from ops.testing.isolation_final_wave_record import FinalWaveStart
from ops.testing.isolation_review_runtime_inputs import (
    ReviewRuntimeInputs,
    load_review_runtime_inputs,
)
from ops.testing.isolation_terminal_publication import publish_terminal_file

if TYPE_CHECKING:
    from ops.testing.isolation_final_wave_cli import FinalWaveInvocation
    from ops.testing.isolation_final_wave_controller import (
        _FinalWaveSession as FinalWaveSessionType,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEDGER = (PROJECT_ROOT / ".omo/evidence/isolation-ledger-phase1a.json").resolve()


@dataclass(slots=True)
class _FinalContext:
    invocation: FinalWaveInvocation
    attempt_id: str
    attempt_root: Path
    control_root: Path
    inputs: Path
    journal: Path
    archive: Path
    runtime: ReviewRuntimeInputs | None
    staging_id: str | None
    session: FinalWaveSessionType
    stage_root: Path | None = None


def run_final_wave_invocation(invocation: FinalWaveInvocation) -> int:
    """Run or replay one exact outer-controller form."""
    context, replay = _prepare(invocation)
    if replay is not None:
        return replay
    if context is None:
        _fail("final-wave context is absent")
    try:
        result = _execute(context)
    except (IsolationError, OSError, ValueError, KeyError):
        _release_staging(context)
        context.session.prepare_failure(utc_now())
        context.session.seal_failure(utc_now())
        _ = publish_final_wave_failure(LEDGER, context.journal)
        return 2
    if result == 0 and invocation.form != "final":
        context.journal.rename(context.archive)
    return result


def _prepare(
    invocation: FinalWaveInvocation,
) -> tuple[_FinalContext | None, int | None]:
    ledger, _ = load_json(LEDGER)
    attempt_id = str(ledger["attempt_id"])
    attempt_root = Path(str(ledger["attempt_root"]))
    journal = attempt_root / "final-wave-controller.json"
    archive = attempt_root / f"final-wave-controller-{invocation.form}.json"
    if archive.exists():
        record, _ = load_json(archive)
        return None, 0 if record.get("state") == "success" else 2
    control = attempt_root.parents[1] / "clinic-os-phase1a-final"
    ensure_final_control_root(control)
    inputs = control / "terminal/inputs.json"
    runtime = (
        load_review_runtime_inputs(inputs, invocation.sha)
        if invocation.form != "inputs"
        else None
    )
    staging_id = str(uuid.uuid4()) if invocation.form == "final" else None
    claim_id = staging_id or (
        scope_claim_id(attempt_id) if invocation.form == "scope-pre" else None
    )
    stage_root = attempt_root / "claims" / claim_id if claim_id is not None else None
    payload = final_wave_payload(invocation, control, inputs, stage_root)
    evidence = (
        final_predecessor(runtime, control)
        if invocation.form in {"pre-f4", "final"}
        else None
    )
    pre_f4 = control / "pre-f4.json"
    start = FinalWaveStart(
        attempt_id,
        invocation.sha,
        invocation.form,
        _boot_id(),
        journal,
        attempt_root / "final-wave-controller.lease",
        barrier_argv_sha256(payload),
        os.getpid(),
        _start_ticks(os.getpid()),
        utc_now(),
        staging_id,
        evidence,
        raw_sha256(pre_f4.read_bytes()) if invocation.form == "final" else None,
    )
    session = acquire_final_wave(start, RecoveryPolicy(_process_is_live))
    context = _FinalContext(
        invocation,
        attempt_id,
        attempt_root,
        control,
        inputs,
        journal,
        archive,
        runtime,
        staging_id,
        session,
    )
    return context, None


def _execute(context: _FinalContext) -> int:
    if context.session.record["state"] != "prepared":
        _fail("final-wave recovery is not terminal")
    _activate_staging(context)
    payload = final_wave_payload(
        context.invocation,
        context.control_root,
        context.inputs,
        context.stage_root,
    )
    if (
        _run_final_child(
            context.session, payload, final_wave_environment(context.runtime)
        )
        != 0
    ):
        _release_staging(context)
        context.session.seal_failure(utc_now())
        _ = publish_final_wave_failure(LEDGER, context.journal)
        return 2
    if context.invocation.form == "scope-pre":
        _complete_scope(context)
    elif context.invocation.form == "final":
        return _complete_final(context)
    elif context.invocation.form == "inputs":
        record_inputs_frozen(LEDGER, raw_sha256(context.inputs.read_bytes()))
    elif context.invocation.form == "pre-f4":
        pre_f4 = context.control_root / "pre-f4.json"
        record_pre_f4_frozen(LEDGER, raw_sha256(pre_f4.read_bytes()))
    context.session.seal_success(utc_now())
    return 0


def _activate_staging(context: _FinalContext) -> None:
    if context.invocation.form not in {"scope-pre", "final"}:
        return
    runtime = _runtime(context)
    claim_id = context.staging_id or scope_claim_id(context.attempt_id)
    if context.invocation.form == "final":
        context.session.reserve_final_staging(utc_now())
    context.stage_root = activate_final_staging(runtime, claim_id)
    if context.invocation.form == "final":
        context.session.activate_final_staging(utc_now())


def _complete_scope(context: _FinalContext) -> None:
    runtime, root = _runtime(context), _stage_root(context)
    _ = publish_terminal_file(
        runtime.ledger_path,
        runtime.publisher_claim_id,
        "f4-pre",
        root / "staging/F4-pre.txt",
        runtime.terminal_root,
    )
    _release_staging(context)


def _complete_final(context: _FinalContext) -> int:
    runtime, root = _runtime(context), _stage_root(context)
    if context.staging_id is None:
        _fail("final staging claim ID is absent")
    decision = root / "staging/F4-final.txt"
    outcome = root / "staging/F4-outcome.json"
    decision_sha = publish_terminal_file(
        runtime.ledger_path,
        runtime.publisher_claim_id,
        "f4-final",
        decision,
        runtime.terminal_root,
    )
    outcome_sha = raw_sha256(outcome.read_bytes())
    approved = decision.read_bytes() == b"APPROVE\n"
    _release_staging(context)
    record_f4_created(runtime.ledger_path, decision_sha, approved=approved)
    if not approved:
        context.session.seal_failure(utc_now())
        _ = publish_final_wave_failure(LEDGER, context.journal)
        return 2
    context.session.advance_final_decision(
        (decision_sha, outcome_sha, decision_sha), utc_now()
    )
    payload = final_wave_payload(
        context.invocation, context.control_root, context.inputs, None
    )
    if _run_final_child(context.session, payload, final_wave_environment(runtime)) != 0:
        context.session.seal_failure(utc_now())
        _ = publish_final_wave_failure(LEDGER, context.journal)
        return 2
    final_path = context.control_root / "final.json"
    record_final_frozen(runtime.ledger_path, raw_sha256(final_path.read_bytes()))
    context.session.seal_success(utc_now())
    return 0


def _release_staging(context: _FinalContext) -> None:
    if context.stage_root is None:
        return
    claim_id = context.staging_id or scope_claim_id(context.attempt_id)
    release_final_staging(_runtime(context), claim_id, context.stage_root)
    context.stage_root = None


def _runtime(context: _FinalContext) -> ReviewRuntimeInputs:
    if context.runtime is None:
        _fail("final-wave runtime inputs are absent")
    return context.runtime


def _stage_root(context: _FinalContext) -> Path:
    if context.stage_root is None:
        _fail("final-wave staging root is absent")
    return context.stage_root


def _fail(message: str) -> Never:
    raise IsolationError(message)
