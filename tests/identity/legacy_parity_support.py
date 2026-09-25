"""Real PostgreSQL fixtures and decision assertions for legacy boundary parity.

No authorization function is replaced. Each invocation runs in a savepoint so
successful commands, audit appends and on_commit callbacks cannot alter the next
case. Both success and denial are observed at the named callable's boundary.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module
from inspect import unwrap
from types import CodeType, FrameType
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.billing.services import BillingAccessDeniedError
from apps.core.integration import _ClinicAuthorityRevokedError, _SubjectIneligibleError
from apps.ehr.services import (
    SOAP_FIELDS,
    ClinicalAccessDeniedError,
    record_clinical_note,
    view_version,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.management.base import LifecycleCommandError
from apps.identity.models import User, UserClinicRole
from apps.intake.access import PatientAccessDeniedError
from apps.retention.services import RetentionAccessDeniedError
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    AvailabilityAccessDeniedError,
)
from apps.teleconsult.services import TeleconsultAccessDeniedError
from apps.tenancy.db import tenant_context
from django.contrib.messages.middleware import MessageMiddleware
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBase
from rest_framework.exceptions import PermissionDenied as ApiPermissionDenied

from auth.stepup_test_support import STEP_UP_NOW, create_role_actor, verified_request
from patient_service_support import runtime_role
from renewal.test_encounters import draft, seed

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.ehr.models import ClinicalDocumentVersion, SpecialtyTemplate
    from apps.scheduling.models import Appointment
    from django.http import HttpRequest

    from rbac_fixtures import RbacGraph

LEGACY = ("owner", "physician", "receptionist", "clinic_admin")
MANAGERS = ("owner", "receptionist", "clinic_admin")
ADMINS = ("owner", "clinic_admin")
PHYSICIAN = ("physician",)
DENIALS = (
    CurrentActorError,
    ClinicalAccessDeniedError,
    PatientAccessDeniedError,
    RetentionAccessDeniedError,
    AppointmentAccessDeniedError,
    AvailabilityAccessDeniedError,
    TeleconsultAccessDeniedError,
    BillingAccessDeniedError,
    PermissionDenied,
    ApiPermissionDenied,
    Http404,
    LifecycleCommandError,
    _ClinicAuthorityRevokedError,
    _SubjectIneligibleError,
)


@dataclass(frozen=True)
class LegacyWorld:
    graph: RbacGraph
    actor: User
    role: str
    appointment: Appointment
    template: SpecialtyTemplate
    version: ClinicalDocumentVersion
    request: HttpRequest

    @property
    def clinic(self) -> UUID:
        return self.graph.clinic_a

    @property
    def encounter(self) -> UUID:
        return self.version.document.encounter_id

    def clinic_for(self, valid: bool) -> UUID:
        return self.clinic if valid else self.graph.clinic_c

    def encounter_for(self, valid: bool) -> UUID:
        return self.encounter if valid else uuid4()


@dataclass(frozen=True)
class Boundary:
    symbol: str
    family: str
    allowed: tuple[str, ...]
    invoke: Callable[[LegacyWorld, bool], object]
    # Boolean predicates and filtered projections carry their own deny value.
    decision: Callable[[object], bool] = lambda result: result is not False


def world(graph: RbacGraph, role: str) -> LegacyWorld:
    appointment, template = seed(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = draft(graph, appointment, template)
        version = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=version.revision,
            content=dict.fromkeys(SOAP_FIELDS, "Sintetico parity"),
        )
        version = view_version(clinic_id=graph.clinic_a, version_id=version.pk)
    actor = (
        User.objects.get(pk=graph.physician)
        if role == "physician"
        else create_role_actor(graph, UserClinicRole.Role(role))
    )
    request = verified_request(actor.pk, verified_at=STEP_UP_NOW)
    request.method = "GET"
    request.path = "/workspace/"
    request.META.update(SERVER_NAME="testserver", SERVER_PORT="80")
    MessageMiddleware(lambda _request: HttpResponse()).process_request(request)
    for prefix in (
        "ehr.encounter",
        "ehr.attachments",
        "ehr.history",
        "prescription.encounter",
    ):
        request.session[f"{prefix}.{graph.clinic_a}"] = str(
            version.document.encounter_id
        )
    return LegacyWorld(graph, actor, role, appointment, template, version, request)


def has_rows(result: object) -> bool:
    return bool(result)


def http_allowed(result: object) -> bool:
    assert isinstance(result, HttpResponseBase)
    return result.status_code < 400 and not (
        result.status_code in (301, 302, 303, 307, 308)
        and "/auth/" in result.headers.get("Location", "")
    )


def target_code(symbol: str) -> CodeType | None:
    if symbol.startswith("clinic_app."):
        return None
    parts = symbol.split("#", 1)[0].split(".")
    for length in range(len(parts) - 1, 1, -1):
        name = ".".join(parts[:length])
        try:
            target = import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
        else:
            value: object = target
            for part in parts[length:]:
                value = getattr(value, part)
            if isinstance(value, property):
                value = value.fget
            assert callable(value), symbol
            code = getattr(unwrap(value), "__code__", None)
            assert isinstance(code, CodeType), symbol
            return code
    raise AssertionError(symbol)


def exercise(boundary: Boundary, subject: LegacyWorld) -> None:
    code = target_code(boundary.symbol)
    for valid in (True, False):
        session = dict(subject.request.session.items())
        user = subject.request.user
        entered = False

        def observe(frame: FrameType, event: str, _arg: object) -> None:
            nonlocal entered
            if event == "call" and frame.f_code is code:
                entered = True

        previous_profile = sys.getprofile()
        with (
            runtime_role(),
            tenant_context(subject.actor.pk, subject.graph.organization_a),
            transaction.atomic(),
        ):
            sys.setprofile(observe)
            try:
                try:
                    result = boundary.invoke(subject, valid)
                except DENIALS:
                    actual = False
                else:
                    actual = boundary.decision(result)
            finally:
                sys.setprofile(previous_profile)
            if valid and code is not None:
                assert entered, (boundary.symbol, "actual callable was not exercised")
            expected = valid and subject.role in boundary.allowed
            assert actual is expected, (boundary.symbol, subject.role, valid, actual)
            transaction.set_rollback(True)
        subject.request.user = user
        subject.request.session.clear()
        subject.request.session.update(session)


@pytest.fixture
def legacy_world(rbac_graph: RbacGraph, legacy_role: str) -> LegacyWorld:
    return world(rbac_graph, legacy_role)
