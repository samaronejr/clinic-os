"""Provider capability lifecycle registry tests.

The registry replaces the permanent ``real_enabled=False`` constants:
``is_live`` composes the activated state, the process data mode and
``require_live_runtime``. These tests exercise the real trigger, grants
and owner CLI; nothing stubs the gate and the suite never flips the
process data mode to live.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from apps.audit.models import SYSTEM_ORG_ID
from apps.providers import lifecycle
from apps.providers.migrations._seed_v2 import CAPABILITY_SEED
from apps.providers.models import (
    ActivationRecord,
    CapabilityApproval,
    CapabilityVersion,
    HealthEvent,
    ProviderCapability,
)
from apps.providers.services import current_version, is_live
from django.core.management import CommandError, call_command
from django.db import connection, connections
from django.db.utils import IntegrityError, ProgrammingError
from django.utils import timezone
from ops.release import activation

from provider_gate_support import (
    APPROVER,
    APPROVER_ARGS,
    activate_capability,
    seed_capabilities,
)

if TYPE_CHECKING:
    from pathlib import Path
    from types import FrameType

    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)

KEY = "video"


def _state(key: str = KEY) -> str:
    version = ProviderCapability.objects.get(
        key=key, clinic_id__isnull=True
    ).current_version
    assert version is not None
    return version.state


def test_seed_records_every_capability_and_plan_selection() -> None:
    seed_capabilities()

    assert ProviderCapability.objects.count() == len(CAPABILITY_SEED)
    for key, (record_ref, _description, candidates) in CAPABILITY_SEED.items():
        capability = ProviderCapability.objects.get(key=key)
        assert capability.clinic_id is None
        assert capability.record_ref == f"2026-09-24-v2/{record_ref}"
        states = sorted(version.state for version in capability.versions.all())
        assert "researched" in states
        if candidates:
            assert states.count("selected_in_plan") == len(candidates)
            assert capability.current_version is not None
            assert capability.current_version.provider == candidates[0][0]
            assert capability.current_version.state == "selected_in_plan"
        else:
            assert states == ["researched"]
            assert capability.current_version is not None
            assert capability.current_version.state == "researched"
        assert all(version.approval_id is None for version in capability.versions.all())


def test_is_live_requires_all_three_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live branch is proven only by a real live environment.

    The injected mapping carries a complete approved activation (built by
    ``ops.release.activation`` itself, never stubbed) so all three
    conditions hold; dropping the activated state or the live runtime
    breaks the composition. The process data mode is never flipped.
    """
    from renewal.test_live_activation import (  # noqa: PLC0415
        _approved_backend_environment,
        _live_environment,
    )
    from renewal.test_release_readiness import _live_bundle  # noqa: PLC0415

    seed_capabilities()
    evidence_root = _live_bundle(tmp_path / "evidence")
    environment = _approved_backend_environment(
        monkeypatch, _live_environment(tmp_path, evidence_root)
    )
    code, report = activation._activate_report(environment)
    assert code == 0, report

    # Condition (i) alone missing: researched state under a live runtime.
    assert is_live(KEY, clinic_id=None, environment=environment) is False

    activate_capability(KEY)
    assert _state() == "activated"
    # All three conditions hold.
    assert is_live(KEY, clinic_id=None, environment=environment) is True

    # Condition (ii) missing: same activated row, non-live mode.
    synthetic = dict(environment)
    synthetic["CLINIC_DATA_MODE"] = "synthetic"
    assert is_live(KEY, clinic_id=None, environment=synthetic) is False

    # Condition (iii) missing: live mode but halted runtime (no record).
    halted = dict(environment)
    halted[activation.ACTIVATION_STATE_ENV] = str(tmp_path / "absent.json")
    assert is_live(KEY, clinic_id=None, environment=halted) is False

    # Malformed arguments fail closed rather than raising, even in live mode.
    assert (
        is_live(KEY, clinic_id="not-a-uuid", environment=environment) is False  # type: ignore[arg-type]
    )
    assert (
        is_live(KEY, clinic_id=None, environment="not-a-mapping") is False  # type: ignore[arg-type]
    )


def test_is_live_snapshots_the_injected_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutable injected mapping cannot smuggle a synthetic no-op.

    ``is_live`` must evaluate the data mode and ``require_live_runtime``
    against ONE immutable snapshot taken up front. Here a profile hook
    flips the caller's dict to ``synthetic`` the moment the real
    ``current_version`` returns - the point where the old code would let
    ``require_live_runtime`` degrade to its documented non-live no-op and
    report True without any live-runtime evidence.
    """
    import sys  # noqa: PLC0415

    from apps.providers import services  # noqa: PLC0415

    seed_capabilities()
    activate_capability(KEY)
    environment: dict[str, str] = {"CLINIC_DATA_MODE": "live"}
    runtime_gate_calls = 0

    def _observer(frame: FrameType, event: str, _arg: object) -> object:
        nonlocal runtime_gate_calls
        if event == "return" and frame.f_code is services.current_version.__code__:
            environment["CLINIC_DATA_MODE"] = "synthetic"
        if event == "call" and frame.f_code is (
            activation.require_live_runtime.__code__
        ):
            runtime_gate_calls += 1
        return _observer

    sys.setprofile(_observer)
    try:
        result = is_live(KEY, clinic_id=None, environment=environment)
    finally:
        sys.setprofile(None)
    # The flip to synthetic must not make the gate report live; and the
    # same snapshot must drive both checks, so the real runtime gate still
    # sees 'live' and fails closed on the missing activation evidence.
    assert environment["CLINIC_DATA_MODE"] == "synthetic"
    assert runtime_gate_calls == 1
    assert result is False


def test_is_live_requires_settings_and_environment_to_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without injection, settings and os.environ must both say live."""
    seed_capabilities()
    activate_capability(KEY)
    with monkeypatch.context() as patched:
        patched.setenv("CLINIC_DATA_MODE", "live")
        # Process env claims live but the suite's settings say synthetic.
        assert is_live(KEY, clinic_id=None) is False
    with monkeypatch.context() as patched:
        patched.delenv("CLINIC_DATA_MODE", raising=False)
        assert is_live(KEY, clinic_id=None) is False


def test_is_live_fails_closed_on_unknown_key_and_scope() -> None:
    seed_capabilities()
    assert is_live("no_such_capability", clinic_id=None) is False
    assert is_live(KEY, clinic_id=uuid4()) is False
    assert current_version("no_such_capability") is None


def test_clinic_override_wins_over_platform_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from renewal.test_live_activation import (  # noqa: PLC0415
        _approved_backend_environment,
        _live_environment,
    )
    from renewal.test_release_readiness import _live_bundle  # noqa: PLC0415

    seed_capabilities()
    clinic_id = uuid4()
    lifecycle.propose_version(
        KEY,
        clinic_id=clinic_id,
        version_input=lifecycle.VersionInput(provider="synthetic-clinic-scoped"),
    )
    activate_capability(KEY)
    evidence_root = _live_bundle(tmp_path / "evidence")
    environment = _approved_backend_environment(
        monkeypatch, _live_environment(tmp_path, evidence_root)
    )
    assert activation._activate_report(environment)[0] == 0

    # Platform row is activated; the clinic override is still researched.
    assert is_live(KEY, clinic_id=None, environment=environment) is True
    assert is_live(KEY, clinic_id=clinic_id, environment=environment) is False
    override = current_version(KEY, clinic_id=clinic_id)
    assert override is not None
    assert override.state == "researched"


def test_lifecycle_walk_and_audit_events() -> None:
    seed_capabilities()
    lifecycle.propose_version(
        "tls_transport",
        version_input=lifecycle.VersionInput(
            provider="synthetic-ingress",
            environment="production",
            state=CapabilityVersion.State.SELECTED_IN_PLAN,
        ),
    )
    assert _state("tls_transport") == "selected_in_plan"
    lifecycle.approve_version("tls_transport", decision=APPROVER)
    assert _state("tls_transport") == "approved_to_test"
    version = lifecycle.activate_version("tls_transport", decision=APPROVER)
    assert version.state == "activated"
    assert ActivationRecord.objects.filter(version=version).count() == 1
    lifecycle.degrade_version(
        "tls_transport", decision=APPROVER, reason="synthetic outage"
    )
    assert _state("tls_transport") == "degraded"
    lifecycle.activate_version("tls_transport", decision=APPROVER)
    assert _state("tls_transport") == "activated"
    lifecycle.revoke_version(
        "tls_transport", decision=APPROVER, reason="synthetic exit"
    )
    assert _state("tls_transport") == "revoked"
    assert HealthEvent.objects.filter(capability__key="tls_transport").count() == 2

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT event_type FROM clinic_app.audit_event "
            "WHERE organization_id = %s AND event_type LIKE 'providers.%%' "
            "ORDER BY seq",
            [str(SYSTEM_ORG_ID)],
        )
        # Every transition appends exactly one event (SC-4): propose at
        # selected_in_plan records the creation plus the entered state;
        # approve, activate, degrade, reactivate and revoke append one
        # event per actual state change, including the walked legs.
        assert [row[0] for row in cursor.fetchall()] == [
            "providers.capability.proposed",
            "providers.capability.selected_in_plan",
            "providers.capability.approved",
            "providers.capability.sandbox",
            "providers.capability.production_authorized",
            "providers.capability.activated",
            "providers.capability.degraded",
            "providers.capability.activated",
            "providers.capability.revoked",
        ]


def test_activate_without_approval_is_denied_by_trigger() -> None:
    seed_capabilities()
    # researched -> activated is not a legal edge; the trigger must deny it.
    with pytest.raises(ProgrammingError, match="provider_transition_denied"):
        lifecycle.activate_version(KEY, decision=APPROVER)
    assert _state() == "selected_in_plan"
    # The rolled-back transaction leaves no approval or activation behind.
    assert CapabilityApproval.objects.count() == 0
    assert ActivationRecord.objects.count() == 0


# The provider-capability SM row, verbatim (plan SM 'Provider capability'):
# researched->selected_in_plan->approved_to_test->sandbox->
# production_authorized->activated<->degraded ; any->revoked (terminal).
_LEGAL_EDGES: Final = {
    ("researched", "selected_in_plan"),
    ("selected_in_plan", "approved_to_test"),
    ("approved_to_test", "sandbox"),
    ("sandbox", "production_authorized"),
    ("production_authorized", "activated"),
    ("activated", "degraded"),
    ("degraded", "activated"),
    *(
        (source, "revoked")
        for source in (
            "researched",
            "selected_in_plan",
            "approved_to_test",
            "sandbox",
            "production_authorized",
            "activated",
            "degraded",
        )
    ),
}
_ALL_STATES: Final = tuple(CapabilityVersion.State.values)


def _version_in_state(key: str, state: str) -> CapabilityVersion:
    """Build a version at ``state`` through the legal owner path."""
    lifecycle.propose_version(
        key, version_input=lifecycle.VersionInput(provider="matrix-probe")
    )
    if state == "selected_in_plan":
        _sql_transition(key, "selected_in_plan")
    elif state == "approved_to_test":
        lifecycle.approve_version(key, decision=APPROVER)
    elif state == "sandbox":
        lifecycle.approve_version(key, decision=APPROVER)
        _sql_transition(key, "sandbox")
    elif state == "production_authorized":
        lifecycle.approve_version(key, decision=APPROVER)
        _sql_transition(key, "sandbox")
        _sql_transition(key, "production_authorized")
    elif state in ("activated", "degraded"):
        lifecycle.approve_version(key, decision=APPROVER)
        lifecycle.activate_version(key, decision=APPROVER)
        if state == "degraded":
            lifecycle.degrade_version(key, decision=APPROVER, reason="probe")
    elif state == "revoked":
        lifecycle.revoke_version(key, decision=APPROVER, reason="probe")
    # researched needs nothing beyond propose.
    version = ProviderCapability.objects.get(key=key).current_version
    assert version is not None
    assert version.state == state
    return version


def _sql_transition(key: str, target: str) -> None:
    """Move ``key``'s current version to ``target`` with raw owner SQL."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.providers_capabilityversion SET state = %s, "
            "updated_at = now() WHERE id = (SELECT current_version_id FROM "
            "clinic_app.providers_providercapability WHERE key = %s)",
            [target, key],
        )


def _matrix_attempt(key: str, version: CapabilityVersion, target: str) -> None:
    """Attempt ``version.state -> target`` as clinic_owner with raw SQL.

    Gated targets need a bound approval, so the attempt binds a fresh
    approval owned by the SAME capability - mirroring what the owner
    lifecycle does - and assert separately whether the edge is legal.
    """
    with connection.cursor() as cursor:
        if version.approval_id is None and target not in (
            "researched",
            "selected_in_plan",
        ):
            # Gated targets need a bound approval owned by the same
            # capability - what the owner lifecycle supplies.
            approval = CapabilityApproval.objects.create(
                capability_id=version.capability_id,
                approver_name="Synthetic Owner",
                approver_role="clinic owner",
                evidence_uri="synthetic://evidence/matrix",
                decided_at=timezone.now(),
            )
            cursor.execute(
                "UPDATE clinic_app.providers_capabilityversion "
                "SET state = %s, approval_id = %s, updated_at = now() "
                "WHERE id = %s",
                [target, str(approval.id), str(version.id)],
            )
        else:
            cursor.execute(
                "UPDATE clinic_app.providers_capabilityversion "
                "SET state = %s, updated_at = now() WHERE id = %s",
                [target, str(version.id)],
            )


def test_full_transition_matrix_is_enforced() -> None:
    """Every from->to pair: legal edges apply, every other edge is P0001."""
    outcome: dict[tuple[str, str], str] = {}
    for index, source in enumerate(_ALL_STATES):
        for target in _ALL_STATES:
            if source == target:
                continue
            key = f"matrix_{index}_{target[:6]}"
            try:
                version = _version_in_state(key, source)
                _matrix_attempt(key, version, target)
            except ProgrammingError as error:
                outcome[source, target] = (
                    "P0001" if "provider_transition_denied" in str(error) else "other"
                )
            else:
                outcome[source, target] = "allowed"
    mismatches = {
        edge: outcome[edge]
        for edge in outcome
        if (edge in _LEGAL_EDGES) != (outcome[edge] == "allowed")
    }
    assert mismatches == {}
    assert len(outcome) == 56


def test_trigger_denies_every_illegal_edge() -> None:
    seed_capabilities()
    version = ProviderCapability.objects.get(key=KEY).current_version
    assert version is not None
    illegal = (
        "approved_to_test",
        "sandbox",
        "production_authorized",
        "activated",
        "degraded",
    )
    for target in illegal:
        with (
            pytest.raises(ProgrammingError, match="provider_transition_denied"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE clinic_app.providers_capabilityversion "
                "SET state = %s, updated_at = now() WHERE id = %s",
                [target, str(version.id)],
            )
    version.refresh_from_db()
    assert version.state == "selected_in_plan"


def test_insert_bypass_is_denied_by_trigger() -> None:
    seed_capabilities()
    capability = ProviderCapability.objects.get(key=KEY)
    # A raw INSERT must not be able to mint an already-activated version.
    for state in ("approved_to_test", "sandbox", "activated"):
        with (
            pytest.raises(ProgrammingError, match="provider_transition_denied"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO clinic_app.providers_capabilityversion "
                "(id, capability_id, provider, account, environment, "
                "api_version, region, retention_terms, state, approval_id, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), %s, 'x', '', '', '', '', '', %s, "
                "NULL, now(), now())",
                [str(capability.id), state],
            )
    # The propose vocabulary still inserts cleanly.
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO clinic_app.providers_capabilityversion "
            "(id, capability_id, provider, account, environment, "
            "api_version, region, retention_terms, state, approval_id, "
            "created_at, updated_at) "
            "VALUES (gen_random_uuid(), %s, 'x', '', '', '', '', '', "
            "'researched', NULL, now(), now())",
            [str(capability.id)],
        )


def test_trigger_requires_approval_for_approved_states() -> None:
    seed_capabilities()
    version = ProviderCapability.objects.get(key=KEY).current_version
    assert version is not None
    with (
        pytest.raises(ProgrammingError, match="provider_transition_denied"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE clinic_app.providers_capabilityversion "
            "SET state = 'approved_to_test', updated_at = now() "
            "WHERE id = %s",
            [str(version.id)],
        )


def test_revoked_is_terminal_and_approval_is_immutable() -> None:
    seed_capabilities()
    activate_capability(KEY)
    lifecycle.revoke_version(KEY, decision=APPROVER, reason="synthetic exit")
    version = current_version(KEY)
    assert version is not None
    assert version.state == "revoked"
    with pytest.raises(ProgrammingError, match="provider_transition_denied"):
        lifecycle.activate_version(KEY, decision=APPROVER)
    approval = CapabilityApproval.objects.filter(capability__key=KEY).first()
    assert approval is not None
    with pytest.raises(IntegrityError), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.providers_capabilityapproval "
            "SET approver_name = 'tampered' WHERE id = %s",
            [str(approval.id)],
        )
    with pytest.raises(IntegrityError), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM clinic_app.providers_capabilityversion WHERE id = %s",
            [str(version.id)],
        )


def test_runtime_role_reads_but_cannot_write(app_database_url: str) -> None:
    seed_capabilities()
    with psycopg.connect(app_database_url, autocommit=True) as runtime:
        rows = runtime.execute(
            "SELECT count(*) FROM clinic_app.providers_capabilityversion"
        ).fetchone()
        assert rows is not None
        assert rows[0] > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute(
                "UPDATE clinic_app.providers_capabilityversion SET state = 'activated'"
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute(
                "INSERT INTO clinic_app.providers_providercapability "
                "(id, key, record_ref) VALUES (gen_random_uuid(), 'x', 'r')"
            )


def test_concurrent_activation_converges_to_one_winner() -> None:
    seed_capabilities()
    lifecycle.approve_version(KEY, decision=APPROVER)
    barrier = Barrier(2)

    def worker() -> str:
        try:
            barrier.wait(timeout=10)
            lifecycle.activate_version(KEY, decision=APPROVER)
        except ProgrammingError as error:
            if "provider_transition_denied" not in str(error):
                raise
            outcome = "denied"
        else:
            outcome = "activated"
        finally:
            connections.close_all()
        return outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = sorted(future.result(timeout=30) for future in futures)
    assert results == ["activated", "denied"]
    assert _state() == "activated"
    assert ActivationRecord.objects.filter(capability__key=KEY).count() == 1


def test_owner_cli_happy_path_and_report() -> None:
    seed_capabilities()
    call_command(
        "provider_capability",
        "propose",
        key="tls_transport",
        provider="synthetic-ingress",
        environment="production",
        state="selected_in_plan",
    )
    call_command("provider_capability", "approve", key="tls_transport", **APPROVER_ARGS)
    call_command(
        "provider_capability", "activate", key="tls_transport", **APPROVER_ARGS
    )
    assert _state("tls_transport") == "activated"
    report = lifecycle.report_markdown()
    assert "| `tls_transport` | platform | activated |" in report
    assert report.startswith("| Capability | Scope | State |")


def test_owner_cli_rejects_malformed_and_missing() -> None:
    seed_capabilities()
    with pytest.raises(CommandError, match="owner lifecycle command failed"):
        call_command("provider_capability", "propose", key="Bad Key!!", provider="x")
    with pytest.raises(CommandError, match="owner lifecycle command failed"):
        call_command(
            "provider_capability", "approve", key="missing_key", **APPROVER_ARGS
        )
    with pytest.raises(CommandError, match="owner lifecycle command failed"):
        call_command(
            "provider_capability",
            "approve",
            key=KEY,
            approver_name="",
            approver_role="owner",
            evidence_uri="synthetic://e",
        )
    # A state outside the propose vocabulary is rejected by the service.
    with pytest.raises(CommandError, match="owner lifecycle command failed"):
        call_command(
            "provider_capability",
            "propose",
            key="tls_transport",
            provider="x",
            state="activated",
        )


def test_runtime_role_cannot_run_owner_cli(app_database_url: str) -> None:
    seed_capabilities()
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
        try:
            with pytest.raises(CommandError, match="owner lifecycle command failed"):
                call_command("provider_capability", "report")
        finally:
            cursor.execute("RESET ROLE")


def test_cross_capability_bindings_are_rejected() -> None:
    """Owner DML cannot bind a version/approval/history across capabilities."""
    seed_capabilities()
    activate_capability(KEY)
    other = "tls_transport"
    lifecycle.propose_version(
        other, version_input=lifecycle.VersionInput(provider="matrix-probe")
    )
    foreign_version = (
        CapabilityVersion.objects.filter(capability__key=other)
        .order_by("-created_at")
        .first()
    )
    assert foreign_version is not None
    foreign_approval = CapabilityApproval.objects.create(
        capability_id=foreign_version.capability_id,
        approver_name="Synthetic Owner",
        approver_role="clinic owner",
        evidence_uri="synthetic://evidence/binding",
        decided_at=timezone.now(),
    )
    capability = ProviderCapability.objects.get(key=KEY)
    version = capability.current_version
    assert version is not None

    # current_version cannot point at another capability's version.
    with (
        pytest.raises(IntegrityError),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE clinic_app.providers_providercapability "
            "SET current_version_id = %s, updated_at = now() WHERE id = %s",
            [str(foreign_version.id), str(capability.id)],
        )
    # A selected_in_plan version cannot bind another capability's
    # approval while advancing to approved_to_test.
    pending = CapabilityVersion.objects.filter(
        capability=capability, state="selected_in_plan"
    ).first()
    if pending is None:
        lifecycle.propose_version(
            KEY,
            version_input=lifecycle.VersionInput(provider="binding-probe"),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.providers_capabilityversion "
                "SET state = 'selected_in_plan', updated_at = now() "
                "WHERE id = (SELECT current_version_id FROM "
                "clinic_app.providers_providercapability WHERE key = %s)",
                [KEY],
            )
        pending = ProviderCapability.objects.get(key=KEY).current_version
        assert pending is not None
        assert pending.state == "selected_in_plan"
    with (
        pytest.raises(IntegrityError),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE clinic_app.providers_capabilityversion "
            "SET state = 'approved_to_test', approval_id = %s, "
            "updated_at = now() WHERE id = %s",
            [str(foreign_approval.id), str(pending.id)],
        )
    # History rows cannot reference foreign versions or approvals.
    with (
        pytest.raises(IntegrityError),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO clinic_app.providers_activationrecord "
            "(id, capability_id, version_id, approval_id, activated_at) "
            "VALUES (gen_random_uuid(), %s, %s, %s, now())",
            [str(capability.id), str(foreign_version.id), str(foreign_approval.id)],
        )
    with (
        pytest.raises(IntegrityError),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO clinic_app.providers_healthevent "
            "(id, capability_id, version_id, approval_id, kind, detail, "
            "recorded_at) VALUES (gen_random_uuid(), %s, %s, %s, "
            "'note', 'probe', now())",
            [str(capability.id), str(version.id), str(foreign_approval.id)],
        )
    # INSERT-time binding on the capability row is checked too.
    with (
        pytest.raises(IntegrityError),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO clinic_app.providers_providercapability "
            "(id, key, clinic_id, record_ref, description, "
            "current_version_id, created_at, updated_at) "
            "VALUES (gen_random_uuid(), 'binding_probe', NULL, '', '', %s, "
            "now(), now())",
            [str(foreign_version.id)],
        )
    # The capability's own version still binds cleanly.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.providers_providercapability "
            "SET current_version_id = %s, updated_at = now() WHERE id = %s",
            [str(version.id), str(capability.id)],
        )


def test_gate_closed_under_suite_mode(settings: SettingsWrapper) -> None:
    """The suite's real synthetic mode keeps an activated row not-live."""
    seed_capabilities()
    activate_capability(KEY)
    assert settings.CLINIC_DATA_MODE == "synthetic"
    assert is_live(KEY, clinic_id=None) is False
    del settings.CLINIC_DATA_MODE
    assert is_live(KEY, clinic_id=None) is False
    settings.CLINIC_DATA_MODE = "bogus"
    assert is_live(KEY, clinic_id=None) is False


def test_propose_replaces_current_version() -> None:
    seed_capabilities()
    activate_capability(KEY)
    lifecycle.propose_version(
        KEY, version_input=lifecycle.VersionInput(provider="synthetic-successor")
    )
    assert _state() == "researched"
    assert is_live(KEY, clinic_id=None) is False
