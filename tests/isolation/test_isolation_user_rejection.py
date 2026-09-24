from __future__ import annotations

from pathlib import Path

import pytest
from ops.testing.isolation_close import close_rejected_attempt
from ops.testing.isolation_common import (
    IsolationError,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_atomic_replace,
)
from ops.testing.isolation_rejection import RejectionRequest, reject_attempt
from ops.testing.isolation_user_fix import (
    UserFixAuthorizationRequest,
    authorize_user_fix,
    build_user_rejection_context,
)

from isolation.isolation_rejection_fixtures import empty_inventory
from isolation.isolation_user_fixtures import user_gate_fixture
from isolation.isolation_user_reboot_fixtures import reboot_user_gate
from isolation_claim_fixtures import FOUNDATION_SHA


def test_user_fix_authorization_publishes_one_intent_and_binding_idempotently(
    tmp_path: Path,
) -> None:
    # Given: a sealed terminal approval with a released publisher and open ledger.
    fixture = user_gate_fixture(tmp_path)
    request = UserFixAuthorizationRequest(
        sha=FOUNDATION_SHA,
        terminal_revalidation=fixture.terminal_path,
        control_root=fixture.control_root,
    )
    ledger_before = fixture.ledger_path.read_bytes()

    # When: USER repair authorization is published and replayed after stdout loss.
    binding_path = authorize_user_fix(
        fixture.ledger_path,
        request,
        inventory_reader=empty_inventory,
    )
    replayed = authorize_user_fix(
        fixture.ledger_path,
        request,
        inventory_reader=empty_inventory,
    )

    # Then: one boot-stable intent and one current-boot binding are immutable.
    ledger, _ = load_json(fixture.ledger_path)
    intent_path = Path(str(ledger["attempt_root"])) / "user-fix-intent.json"
    intent, intent_raw = load_json(intent_path)
    binding, _ = load_json(binding_path)
    assert replayed == binding_path
    assert fixture.ledger_path.read_bytes() == ledger_before
    assert intent == {
        "approvals_sha256": fixture.approvals_sha256,
        "attempt_id": ledger["attempt_id"],
        "authorized_reason": "USER:user-requested-fix",
        "final_sha256": fixture.final_sha256,
        "schema_version": 1,
        "sha": FOUNDATION_SHA,
        "tree_sha": fixture.tree_sha,
    }
    assert binding == {
        "approvals_sha256": fixture.approvals_sha256,
        "attempt_id": ledger["attempt_id"],
        "boot_changed": False,
        "current_boot_id": ledger["boot_id"],
        "schema_version": 1,
        "sha": FOUNDATION_SHA,
        "terminal_revalidation_relative_path": (
            f"terminal-revalidation/{ledger['boot_id']}.json"
        ),
        "terminal_revalidation_sha256": raw_sha256(fixture.terminal_path.read_bytes()),
        "user_fix_intent_relative_path": "user-fix-intent.json",
        "user_fix_intent_sha256": raw_sha256(intent_raw),
    }


def test_user_context_reject_and_close_bind_exact_terminal_evidence(
    tmp_path: Path,
) -> None:
    # Given: the current boot's authenticated USER repair binding.
    fixture = user_gate_fixture(tmp_path)
    binding_path = authorize_user_fix(
        fixture.ledger_path,
        UserFixAuthorizationRequest(
            sha=FOUNDATION_SHA,
            terminal_revalidation=fixture.terminal_path,
            control_root=fixture.control_root,
        ),
        inventory_reader=empty_inventory,
    )

    # When: USER context, immutable rejection, and atomic close complete.
    context = build_user_rejection_context(
        fixture.ledger_path,
        sha=FOUNDATION_SHA,
        control_root=fixture.control_root,
        user_authorization=binding_path,
        inventory_reader=empty_inventory,
    )
    digest = reject_attempt(
        fixture.ledger_path,
        RejectionRequest(
            sha=FOUNDATION_SHA,
            reasons=("USER:user-requested-fix",),
            inputs_sha256=context[1] if context[1] != "none" else None,
            pre_f4_sha256=context[2] if context[2] != "none" else None,
            final_sha256=context[3] if context[3] != "none" else None,
            user_authorization=binding_path,
        ),
        inventory_reader=empty_inventory,
    )
    close_rejected_attempt(
        fixture.ledger_path,
        inventory_reader=empty_inventory,
        user_authorization=binding_path,
    )

    # Then: USER is the sole source-fix reason and close binds all three artifacts.
    ledger, _ = load_json(fixture.ledger_path)
    attempt_root = Path(str(ledger["attempt_root"]))
    spec, spec_raw = load_json(attempt_root / "rejection-spec.json")
    assert context[8:] == ("none", "USER:user-requested-fix")
    assert digest == raw_sha256(spec_raw)
    assert spec["retry_kind"] == "source-fix"
    assert spec["failure_receipts_sha256"] is None
    assert spec["rejections"] == [
        {"failure_class": "user-requested-fix", "lane": "USER"}
    ]
    assert ledger["state"] == "closed"
    close_context = ledger["rejection_close"]
    assert isinstance(close_context, dict)
    assert close_context["rejection_spec_sha256"] == digest
    assert close_context["user_fix_intent_sha256"] == spec["user_fix_intent_sha256"]
    assert close_context["user_fix_binding_sha256"] == raw_sha256(
        binding_path.read_bytes()
    )
    assert close_context["terminal_revalidation_sha256"] == raw_sha256(
        fixture.terminal_path.read_bytes()
    )


def test_user_binding_rejects_any_post_terminal_ledger_mutation(
    tmp_path: Path,
) -> None:
    # Given: a current USER binding whose terminal artifact names exact ledger bytes.
    fixture = user_gate_fixture(tmp_path)
    binding_path = authorize_user_fix(
        fixture.ledger_path,
        UserFixAuthorizationRequest(
            sha=FOUNDATION_SHA,
            terminal_revalidation=fixture.terminal_path,
            control_root=fixture.control_root,
        ),
        inventory_reader=empty_inventory,
    )
    ledger, _ = load_json(fixture.ledger_path)
    ledger["last_verified_at_utc"] = "2026-07-16T23:59:59.000001Z"
    write_atomic_replace(fixture.ledger_path, canonical_bytes(ledger))

    # When / Then: the stale binding cannot authenticate a mutated open ledger.
    with pytest.raises((IsolationError, OSError), match=r"ledger|JSON"):
        build_user_rejection_context(
            fixture.ledger_path,
            sha=FOUNDATION_SHA,
            control_root=fixture.control_root,
            user_authorization=binding_path,
            inventory_reader=empty_inventory,
        )


def test_user_intent_survives_reboot_with_new_binding_and_same_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: USER intent, binding, and rejection spec published on creation boot.
    fixture = user_gate_fixture(tmp_path)
    first_binding = authorize_user_fix(
        fixture.ledger_path,
        UserFixAuthorizationRequest(
            FOUNDATION_SHA,
            fixture.terminal_path,
            fixture.control_root,
        ),
        inventory_reader=empty_inventory,
    )
    first_context = build_user_rejection_context(
        fixture.ledger_path,
        sha=FOUNDATION_SHA,
        control_root=fixture.control_root,
        user_authorization=first_binding,
        inventory_reader=empty_inventory,
    )
    first_digest = reject_attempt(
        fixture.ledger_path,
        RejectionRequest(
            FOUNDATION_SHA,
            ("USER:user-requested-fix",),
            first_context[1],
            first_context[2],
            first_context[3],
            first_binding,
        ),
        inventory_reader=empty_inventory,
    )

    # When: terminal reconciliation advances to a new boot and USER resumes.
    reboot_terminal = reboot_user_gate(fixture, tmp_path, monkeypatch)
    second_binding = authorize_user_fix(
        fixture.ledger_path,
        UserFixAuthorizationRequest(
            FOUNDATION_SHA,
            reboot_terminal,
            fixture.control_root,
        ),
        inventory_reader=empty_inventory,
    )
    second_context = build_user_rejection_context(
        fixture.ledger_path,
        sha=FOUNDATION_SHA,
        control_root=fixture.control_root,
        user_authorization=second_binding,
        inventory_reader=empty_inventory,
    )
    second_digest = reject_attempt(
        fixture.ledger_path,
        RejectionRequest(
            FOUNDATION_SHA,
            ("USER:user-requested-fix",),
            second_context[1],
            second_context[2],
            second_context[3],
            second_binding,
        ),
        inventory_reader=empty_inventory,
    )

    # Then: intent/spec are unchanged, old binding is stale, and close uses new T.
    assert second_digest == first_digest
    assert second_binding != first_binding
    assert second_context[8] == str(reboot_terminal)
    with pytest.raises(IsolationError, match="current boot binding"):
        build_user_rejection_context(
            fixture.ledger_path,
            sha=FOUNDATION_SHA,
            control_root=fixture.control_root,
            user_authorization=first_binding,
            inventory_reader=empty_inventory,
        )
    close_rejected_attempt(
        fixture.ledger_path,
        inventory_reader=empty_inventory,
        user_authorization=second_binding,
        terminal_revalidation=reboot_terminal,
    )
    ledger, _ = load_json(fixture.ledger_path)
    close_context = ledger["rejection_close"]
    assert isinstance(close_context, dict)
    assert close_context["terminal_revalidation_sha256"] == raw_sha256(
        reboot_terminal.read_bytes()
    )
