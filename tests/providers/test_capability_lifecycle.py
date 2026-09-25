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
from typing import TYPE_CHECKING
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
from ops.release import activation

from provider_gate_support import (
    APPROVER,
    APPROVER_ARGS,
    activate_capability,
    seed_capabilities,
)

if TYPE_CHECKING:
    from pathlib import Path

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
        assert [row[0] for row in cursor.fetchall()] == [
            "providers.capability.proposed",
            "providers.capability.approved",
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
