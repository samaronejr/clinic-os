"""Real operator, audit, migration and crypto boundaries (not staff exemptions)."""

from __future__ import annotations

import io
import os
import pty
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.audit import services as audit
from apps.audit.canonical import AuditEventInput
from apps.core import fairness, telemetry
from apps.ehr.management.commands.encrypt_attachment_objects import Command
from apps.identity.management import base
from apps.identity.management.bootstrap import BootstrapRequest, bootstrap_clinic
from apps.identity.phase1a_identity_acl_migration import remove_runtime_identity_acl
from apps.tenancy import envelope
from django.db import connection
from django.db.migrations.state import ProjectState
from django.utils import timezone

from identity.legacy_parity_support import http_allowed
from identity.nonstaff_differential import DifferentialProbe

if TYPE_CHECKING:
    import pytest
    from django.http import HttpRequest

    from identity.nonstaff_subjects import NonstaffSubjects


def _tty(monkeypatch: pytest.MonkeyPatch) -> bool:
    master, slave = pty.openpty()
    try:
        with monkeypatch.context() as patch:
            # Only redirect the terminal device. isatty and the real database
            # owner check are left intact, including the runtime deny control.
            patch.setattr(base, "Path", lambda _name: Path(os.ttyname(slave)))
            with base.owner_tty() as tty:
                return os.isatty(tty.fileno())
    finally:
        os.close(slave)
        os.close(master)


def _reverse_acl() -> None:
    # Real DDL in the replay savepoint: its function/drop/grant effects are
    # rolled back before another scenario or actor state is exercised.
    with connection.schema_editor(atomic=False) as editor:
        remove_runtime_identity_acl(ProjectState().apps, editor)


def infrastructure_probes(
    d: NonstaffSubjects, monkeypatch: pytest.MonkeyPatch
) -> list[DifferentialProbe]:
    w = d.legacy
    event = AuditEventInput(
        "synthetic.differential",
        "clinic-os-ops",
        None,
        "synthetic.subject",
        str(w.clinic),
        timezone.now(),
    )
    bootstrap = BootstrapRequest(
        uuid4(),
        "Sintetico",
        "12345678000199",
        uuid4(),
        "Sintetico",
        "SP",
        "America/Sao_Paulo",
        uuid4(),
        "synthetic-differential-bootstrap",
        "synthetic@example.invalid",
    )
    credential = uuid4().hex + uuid4().hex
    probes = [
        DifferentialProbe(
            "apps.audit.services.record_event",
            lambda: audit.record_event(event, payload={}),
        ),
        DifferentialProbe(
            "apps.audit.services._record_system_event",
            lambda: audit._record_system_event(event, payload={}),
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.core.fairness._organization_quotas",
            lambda: fairness._organization_quotas(w.graph.organization_a),
        ),
        DifferentialProbe(
            "apps.ehr.management.commands.encrypt_attachment_objects.Command.handle",
            lambda: Command(stdout=io.StringIO()).handle(
                organization_id=[str(w.graph.organization_a)]
            ),
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.identity.management.base.owner_tty",
            lambda: _tty(monkeypatch),
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.identity.management.bootstrap.bootstrap_clinic",
            lambda: bootstrap_clinic(bootstrap, credential),
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.identity.phase1a_identity_acl_migration.remove_runtime_identity_acl",
            _reverse_acl,
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.tenancy.envelope.issue_tenant_key",
            envelope.issue_tenant_key,
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.tenancy.envelope.encrypt",
            lambda: envelope.encrypt(
                purpose="synthetic.differential", plaintext=b"Sintetico"
            ),
            bool,
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.tenancy.envelope.decrypt",
            lambda: (
                envelope.decrypt(purpose="synthetic.differential", envelope=d.encrypted)
                == b"Sintetico"
            ),
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.tenancy.envelope.reencrypt",
            lambda: envelope.reencrypt(
                purpose="synthetic.differential", envelope=d.encrypted
            ),
            bool,
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.tenancy.envelope.protect",
            lambda: envelope.protect(
                purpose="synthetic.differential", plaintext=b"Sintetico"
            ),
            bool,
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.tenancy.envelope.reveal",
            lambda: (
                envelope.reveal(purpose="synthetic.differential", envelope=d.encrypted)
                == b"Sintetico"
            ),
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.tenancy.envelope.rewrap_tenant_keys",
            lambda: envelope.rewrap_tenant_keys(new_kek="ab" * 32),
            bool,
            database_role="clinic_owner",
        ),
        DifferentialProbe(
            "apps.tenancy.envelope.tenant_key_status",
            envelope.tenant_key_status,
            bool,
            database_role="clinic_owner",
        ),
    ]
    owner_gates = {
        "apps.audit.services._record_system_event",
        "apps.ehr.management.commands.encrypt_attachment_objects.Command.handle",
        "apps.identity.management.base.owner_tty",
    }
    return [
        *probes,
        *(
            replace(p, database_role="clinic_app", expected=False)
            for p in probes
            if p.symbol in owner_gates
        ),
    ]


def metrics_probes(
    d: NonstaffSubjects, monkeypatch: pytest.MonkeyPatch
) -> list[DifferentialProbe]:
    monkeypatch.setenv(telemetry.OPS_METRICS_TOKEN_ENV, d.ops_value)
    monkeypatch.setenv(telemetry.OPS_METRICS_NETWORKS_ENV, "192.0.2.0/28")

    def request(*, permitted: bool) -> HttpRequest:
        result = d.request()
        result.META.update(
            REMOTE_ADDR="192.0.2.3" if permitted else "192.0.2.80",
            HTTP_AUTHORIZATION=f"Bearer {d.ops_value}"
            if permitted
            else "Bearer invalid",
        )
        return result

    def empty_network() -> object:
        with monkeypatch.context() as patch:
            patch.setenv(telemetry.OPS_METRICS_NETWORKS_ENV, "invalid")
            return telemetry._allowed_networks()

    return [
        DifferentialProbe(
            "apps.core.telemetry._allowed_networks", telemetry._allowed_networks, bool
        ),
        DifferentialProbe(
            "apps.core.telemetry._allowed_networks", empty_network, bool, expected=False
        ),
        DifferentialProbe(
            "apps.core.telemetry._client_ip_allowed",
            lambda: telemetry._client_ip_allowed(request(permitted=True)),
        ),
        DifferentialProbe(
            "apps.core.telemetry._client_ip_allowed",
            lambda: telemetry._client_ip_allowed(request(permitted=False)),
            expected=False,
        ),
        DifferentialProbe(
            "apps.core.telemetry._ops_token_valid",
            lambda: telemetry._ops_token_valid(request(permitted=True)),
        ),
        DifferentialProbe(
            "apps.core.telemetry._ops_token_valid",
            lambda: telemetry._ops_token_valid(request(permitted=False)),
            expected=False,
        ),
        DifferentialProbe(
            "apps.core.telemetry.internal_metrics",
            lambda: telemetry.internal_metrics(request(permitted=True)),
            http_allowed,
        ),
        DifferentialProbe(
            "apps.core.telemetry.internal_metrics",
            lambda: telemetry.internal_metrics(request(permitted=False)),
            http_allowed,
            expected=False,
        ),
    ]
