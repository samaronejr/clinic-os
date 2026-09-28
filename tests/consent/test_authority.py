"""Every consent/notice guard decides through todo 6's ``has_permission``.

Guards are derived from ``apps/consent`` source (every function that calls
``require_permission``), never listed by hand. Each guard runs as clinic_app
under every ``UserClinicRole`` role. The permitted set comes from
``BUNDLES_V1``, and the expected permission names are this module's own
literals, never read from the product. Each cell records three things: the
exact names reaching ``clinic_app.has_permission`` (a spy), the exact refusal,
and the rows written. A refusal must write nothing. Inactive, foreign-clinic,
narrowed-bundle and relation cells vary every input the guards read.
"""

from __future__ import annotations

import ast
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.consent import services as consent
from apps.consent import views as consent_views
from apps.consent.models import (
    AIUseDisclosure,
    ConsentAcceptance,
    ConsentRevocation,
    ConsentText,
    NoticeVersion,
    ParticipantAcknowledgment,
    RefusalRecord,
)
from apps.ehr.models import Encounter
from apps.identity import current_context
from apps.identity.models import (
    CareTeamMembership,
    ProfessionalRegistration,
    RoleGrant,
    User,
    UserClinicRole,
)
from apps.identity.otp import is_confirmed_verified_user
from apps.identity.permissions import BUNDLES_V1
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.intake.patient_access import patient_session_context
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.http import HttpResponse
from django.utils import timezone
from psycopg import sql as psql

from auth.stepup_test_support import STEP_UP_NOW, verified_request
from consent.test_purposes import _encounter, _patient_session
from identity.permission_support import owner_context
from otp_test_support import current_user_guc
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from renewal.test_consent import accept as accept_offered
from renewal.test_encounters import seed as clinical_seed
from renewal.test_retention import staff_client

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from django.db.models import Model

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[2]
ROLES = tuple(UserClinicRole.Role.values)
PUBLISH = ("configuration.clinic", "configuration.organization")
READ = ("demographics.read", "configuration.organization")
PAGE = ("demographics.read", "configuration.clinic", "configuration.organization")
CLINICAL = ("clinical.write",)
# The specification: guard -> permission names in the order they must reach
# has_permission. Patient-scoped guards pass the encounter patient's enrollment.
SPEC: dict[str, tuple[str, ...]] = {
    "apps.consent.services.publish_text": PUBLISH,
    "apps.consent.services.publish_notice": PUBLISH,
    "apps.consent.services.staff_receipts": READ,
    "apps.consent.services.staff_refusals": READ,
    "apps.consent.services.consent_for_future_use": READ,
    "apps.consent.services.ai_disclosure_status": READ,
    "apps.consent.services.record_ai_disclosure": CLINICAL,
    "apps.consent.services.acknowledge_participant": CLINICAL,
    "apps.consent.views.staff_consent": PAGE,
}
COUNCIL = {"physician": "CRM", "nurse": "COREN", "allied_professional": "CRP"}
UNAUTHORIZED = ("current actor unauthorized",)
UNAVAILABLE = ("current actor unavailable",)
LEGACY_ROLE_HELPERS = re.compile(
    r"require_current_actor_clinic_roles|has_clinic_role|questionnaire_staff|"
    r"clinics_for_user_roles|UserClinicRole\.objects"
)


def permitted(permissions: Sequence[str]) -> frozenset[str]:
    return frozenset(
        role
        for role, bundle in BUNDLES_V1.items()
        if any(permission in bundle for permission in permissions)
    )


def expected_names(permissions: Sequence[str], held: frozenset[str]) -> list[str]:
    """Names reaching has_permission: every one up to the first held."""
    names: list[str] = []
    for permission in permissions:
        names.append(permission)
        if permission in held:
            break
    return names


def derived_guards() -> set[str]:
    found: set[str] = set()
    for path in sorted((ROOT / "apps/consent").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        module = str(path.relative_to(ROOT))[:-3].replace("/", ".")
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "require_permission"
                for call in ast.walk(node)
            ):
                found.add(f"{module}.{node.name}")
    return found


@dataclass(frozen=True)
class World:
    graph: RbacGraph
    encounter: UUID
    enrollment: UUID
    other_enrollment: UUID

    @property
    def clinic(self) -> UUID:
        return self.graph.clinic_a

    @property
    def organization(self) -> UUID:
        return self.graph.organization_a


def _registration(w: World, user: UUID, role: str, clinic: UUID) -> None:
    ProfessionalRegistration.objects.create(
        organization_id=w.organization,
        clinic_id=clinic,
        user_id=user,
        role=role,
        council=COUNCIL[role],
        number="SINTETICO-020",
        jurisdiction="SP" if clinic == w.graph.clinic_a else "RJ",
        specialty="Sintetico",
        status="regular",
        valid_from=timezone.now() - timedelta(days=1),
        valid_to=timezone.now() + timedelta(days=1),
    )


def _care_team(w: World, user: UUID, role: str, enrollment: UUID) -> None:
    CareTeamMembership.objects.create(
        organization_id=w.organization,
        clinic_id=w.clinic,
        patient_enrollment_id=enrollment,
        user_id=user,
        role=role,
        valid_from=timezone.now() - timedelta(days=1),
        valid_to=timezone.now() + timedelta(days=1),
    )


def member(
    w: World,
    role: str,
    *,
    clinic: UUID | None = None,
    registered: bool = True,
    care_team: UUID | None = None,
) -> UUID:
    """Create one actor with a single role; professionals get full scope."""
    user = User.objects.create(
        username=f"synthetic-consent-{role}-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
    )
    target = w.clinic if clinic is None else clinic
    with owner_context(w.organization):
        UserClinicRole.objects.create(
            organization_id=w.organization, clinic_id=target, user=user, role=role
        )
        if role in COUNCIL and registered:
            _registration(w, user.pk, role, target)
            if care_team is not None:
                _care_team(w, user.pk, role, care_team)
    return user.pk


def seed_world(graph: RbacGraph) -> World:
    appointment, _ = clinical_seed(graph)
    encounter = _encounter(graph, appointment)
    with owner_context(graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=graph.clinic_a, patient_id=appointment.patient_id
        ).pk
        patient = Patient.objects.create(
            organization_id=graph.organization_a,
            full_name="Sintetico Consent Outro",
            birth_date=date(1991, 2, 3),
        )
        other = PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"c" * 32,
        ).pk
    # ``_encounter`` registered its own (assigned) physician.
    w = World(graph, encounter, enrollment, other)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        consent.publish_text(
            clinic_id=graph.clinic_a, purpose="teleconsultation", text="Sintetico"
        )
    return w


def view_call(clinic: UUID, actor: UUID) -> Callable[[], object]:
    """Run the decorated view; the step-up device is created before the cell."""
    request = verified_request(actor, verified_at=STEP_UP_NOW)
    request.method = "GET"
    request.path = f"/clinics/{clinic}/consent/"
    request.META.update(SERVER_NAME="testserver", SERVER_PORT="80")
    # Resolve the lazy OTP user and its user-bound device up front, so the
    # cell measures only the view.
    with transaction.atomic(), runtime_role(), current_user_guc(actor):
        assert isinstance(request.user, User)
        assert is_confirmed_verified_user(request.user)

    def call() -> object:
        response = consent_views.staff_consent(request, clinic)
        assert isinstance(response, HttpResponse)
        if response.status_code == 403:
            raise ViewRefusedError(response.content)
        assert response.status_code == 200, response.status_code
        return response.status_code

    return call


class ViewRefusedError(Exception):
    """The staff page rendered its 403 refusal."""


def calls(
    w: World, view: Callable[[], object], clinic: UUID, encounter: UUID
) -> dict[str, Callable[[], object]]:
    """Valid inputs for every guard; ``clinic``/``encounter`` vary per cell."""
    return {
        "apps.consent.services.publish_text": lambda: consent.publish_text(
            clinic_id=clinic, purpose="marketing", text="Sintetico matriz"
        ),
        "apps.consent.services.publish_notice": lambda: consent.publish_notice(
            clinic_id=clinic, topic="ai_use", text="Sintetico aviso"
        ),
        "apps.consent.services.staff_receipts": lambda: consent.staff_receipts(
            clinic_id=clinic, enrollment_id=w.enrollment
        ),
        "apps.consent.services.staff_refusals": lambda: consent.staff_refusals(
            clinic_id=clinic, enrollment_id=w.enrollment
        ),
        "apps.consent.services.consent_for_future_use": (
            lambda: consent.consent_for_future_use(
                clinic_id=clinic, enrollment_id=w.enrollment, purpose="marketing"
            )
        ),
        "apps.consent.services.ai_disclosure_status": (
            lambda: consent.ai_disclosure_status(
                clinic_id=clinic, encounter_id=encounter
            )
        ),
        "apps.consent.services.record_ai_disclosure": (
            lambda: consent.record_ai_disclosure(
                clinic_id=clinic, encounter_id=encounter, informed=True, refused=True
            )
        ),
        "apps.consent.services.acknowledge_participant": (
            lambda: consent.acknowledge_participant(
                clinic_id=clinic, session_id=encounter, participant_kind="interpreter"
            )
        ),
        "apps.consent.views.staff_consent": view,
    }


@dataclass
class Cell:
    error: Exception | None = None
    names: list[tuple[object, ...]] = field(default_factory=list)
    writes: int = 0


def _writes() -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(pg_catalog.sum("
            "n_tup_ins + n_tup_upd + n_tup_del), 0)::bigint "
            "FROM pg_catalog.pg_stat_xact_user_tables WHERE schemaname='clinic_app'"
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _bind(actor: UUID, organization: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true), "
            "pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(actor), str(organization)],
        )


@contextmanager
def as_owner() -> Iterator[None]:
    """Owner-side fixture change inside the running clinic_app transaction."""
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")


def run(actor: UUID, organization: UUID, call: Callable[[], object]) -> Cell:
    """Execute one cell in a rolled-back savepoint and observe it."""
    cell = Cell()

    def spy(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object],
        many: bool,
        context: object,
    ) -> object:
        if "clinic_app.has_permission(" in sql:
            cell.names.append(tuple(params))
        return execute(sql, params, many, context)

    _bind(actor, organization)
    before = _writes()
    try:
        with transaction.atomic(), connection.execute_wrapper(spy):
            call()
            transaction.set_rollback(True)
    except (
        current_context.CurrentActorError,
        PatientAccessDeniedError,
        ViewRefusedError,
        ValidationError,
        DatabaseError,
    ) as error:
        cell.error = error
    cell.writes = _writes() - before
    return cell


@contextmanager
def matrix_transaction(w: World) -> Iterator[None]:
    with runtime_role(), transaction.atomic():
        _bind(w.graph.clinic_admin, w.organization)
        yield
        transaction.set_rollback(True)


def subject_enrollment(w: World, actor: UUID, encounter: UUID) -> UUID | None:
    """The enrollment the guard can derive: the live encounter RLS decides."""
    _bind(actor, w.organization)
    visible = Encounter.objects.filter(clinic_id=w.clinic, pk=encounter).exists()
    return w.enrollment if visible else None


def names_for(
    guard: str, held: frozenset[str], clinic: UUID, enrollment: UUID | None
) -> list[tuple[object, ...]]:
    return [
        (name, clinic, enrollment if SPEC[guard] == CLINICAL else None)
        for name in expected_names(SPEC[guard], held)
    ]


def assert_refused(
    guard: str, cell: Cell, reference: bytes, args: tuple[str, ...] = UNAUTHORIZED
) -> None:
    """Exact refusal and no row written anywhere in clinic_app."""
    assert cell.writes == 0, (guard, cell.writes)
    if guard == "apps.consent.views.staff_consent":
        assert type(cell.error) is ViewRefusedError, (guard, cell.error)
        assert cell.error.args == (reference,)
    elif args == UNAVAILABLE:
        assert type(cell.error) is current_context._UnavailableActorError, guard
        assert cell.error.args == args
    else:
        assert type(cell.error) is current_context._UnauthorizedActorError, (
            guard,
            cell.error,
        )
        assert cell.error.args == args


def refusal_page(w: World, actor: UUID) -> bytes:
    """The unknown-clinic refusal every staff-page refusal must equal."""
    call = view_call(uuid4(), actor)
    with matrix_transaction(w):
        cell = run(actor, w.organization, call)
    assert type(cell.error) is ViewRefusedError
    assert cell.writes == 0
    return bytes(cell.error.args[0])


def test_guards_are_derived_from_source_and_legacy_role_checks_are_gone() -> None:
    assert derived_guards() == set(SPEC)
    for path in sorted((ROOT / "apps/consent").rglob("*.py")):
        if "migrations" not in path.parts:
            assert not LEGACY_ROLE_HELPERS.search(path.read_text()), path
    assert set(BUNDLES_V1) == set(ROLES)
    for guard, permissions in SPEC.items():
        allowed = permitted(permissions)
        assert allowed, guard
        if permissions == PAGE:
            # Any consent permission opens the page; each action rechecks.
            assert allowed == set(ROLES)
        else:
            # Both sides non-empty: the matrix proves grants and refusals.
            assert set(ROLES) - allowed, guard


def test_database_policies_and_trigger_check_the_same_bundle_names() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename, policyname, "
            "coalesce(qual, '') || ' ' || "
            "coalesce(with_check, '') "
            "FROM pg_catalog.pg_policies WHERE schemaname='clinic_app' "
            "AND tablename LIKE 'consent\\_%' AND 'clinic_app' = ANY(roles)"
        )
        policies = cursor.fetchall()
        cursor.execute(
            "SELECT pg_catalog.pg_get_functiondef("
            "'clinic_app.consent_guard()'::regprocedure)"
        )
        row = cursor.fetchone()
    assert row is not None
    bodies = [str(row[0]), *(str(item[2]) for item in policies)]
    assert len(policies) == 14
    for body in bodies:
        assert "questionnaire_staff" not in body
    names = {
        name
        for body in bodies
        for name in re.findall(r"has_permission\('([a-z_.]+)'", body)
    }
    assert names == {name for permissions in SPEC.values() for name in permissions}


def test_every_guard_refuses_every_catalog_role_without_its_permission(
    rbac_graph: RbacGraph,
) -> None:
    w = seed_world(rbac_graph)
    actors = {role: member(w, role, care_team=w.enrollment) for role in ROLES}
    nobody = User.objects.create(username=f"synthetic-consent-none-{uuid4().hex}").pk
    reference = {actor: refusal_page(w, actor) for actor in (*actors.values(), nobody)}
    views = {
        (actor, w.clinic): view_call(w.clinic, actor)
        for actor in (*actors.values(), nobody)
    }
    decisions = 0
    with matrix_transaction(w):
        for role, actor in actors.items():
            held = BUNDLES_V1[role]
            for guard, call in calls(
                w, views[actor, w.clinic], w.clinic, w.encounter
            ).items():
                cell = run(actor, w.organization, call)
                decisions += 1
                allowed = role in permitted(SPEC[guard])
                assert cell.names == names_for(
                    guard, held, w.clinic, subject_enrollment(w, actor, w.encounter)
                ), (
                    guard,
                    role,
                )
                if allowed:
                    assert cell.error is None, (guard, role, cell.error)
                    if guard.endswith(("publish_text", "record_ai_disclosure")):
                        # Positive control: the write counter sees real writes.
                        assert cell.writes > 0, guard
                else:
                    assert_refused(guard, cell, reference[actor])
        # A user with no clinic role reaches every name and is refused.
        for guard, call in calls(
            w, views[nobody, w.clinic], w.clinic, w.encounter
        ).items():
            cell = run(nobody, w.organization, call)
            decisions += 1
            assert cell.names == names_for(
                guard, frozenset(), w.clinic, subject_enrollment(w, nobody, w.encounter)
            )
            assert_refused(guard, cell, reference[nobody])
    assert decisions == (len(ROLES) + 1) * len(SPEC)


def test_inactive_foreign_clinic_and_narrowed_bundles_are_refused(
    rbac_graph: RbacGraph,
) -> None:
    w = seed_world(rbac_graph)
    clinic_b = w.graph.clinic_b
    cases: list[tuple[str, str]] = [
        (guard, role) for guard in SPEC for role in sorted(permitted(SPEC[guard]))
    ]
    actors = {role: member(w, role, care_team=w.enrollment) for role in ROLES}
    foreign = {role: member(w, role, clinic=clinic_b) for role in ROLES}
    reference = {
        actor: refusal_page(w, actor) for actor in (*actors.values(), *foreign.values())
    }
    views = {
        (actor, clinic): view_call(clinic, actor)
        for actor in actors.values()
        for clinic in (w.clinic, clinic_b)
    } | {(actor, w.clinic): view_call(w.clinic, actor) for actor in foreign.values()}
    narrowed = 0
    with matrix_transaction(w):
        for guard, role in cases:
            actor = actors[role]
            held = BUNDLES_V1[role]
            # Inactive: the same permitted actor, deactivated, is unavailable.
            with transaction.atomic():
                with as_owner():
                    User.objects.filter(pk=actor).update(is_active=False)
                cell = run(
                    actor,
                    w.organization,
                    calls(w, views[actor, w.clinic], w.clinic, w.encounter)[guard],
                )
                transaction.set_rollback(True)
            assert cell.names == []
            assert_refused(guard, cell, reference[actor], UNAVAILABLE)
            # The same role held only in another clinic of the organization.
            other = foreign[role]
            cell = run(
                other,
                w.organization,
                calls(w, views[other, w.clinic], w.clinic, w.encounter)[guard],
            )
            assert cell.names == names_for(
                guard, frozenset(), w.clinic, subject_enrollment(w, other, w.encounter)
            )
            assert_refused(guard, cell, reference[other])
            # A permitted clinic A actor naming clinic B.
            cell = run(
                actor,
                w.organization,
                calls(w, views[actor, clinic_b], clinic_b, w.encounter)[guard],
            )
            assert cell.names == names_for(guard, frozenset(), clinic_b, None)
            assert_refused(guard, cell, reference[actor])
            # Remove each permission the control actually used, one at a time.
            for removed in (name for name in SPEC[guard] if name in held):
                with transaction.atomic():
                    with as_owner():
                        RoleGrant.objects.create(
                            organization_id=w.organization,
                            clinic_id=w.clinic,
                            role=role,
                            permission=removed,
                            valid_from=timezone.now() - timedelta(days=1),
                        )
                    remaining = held - {removed}
                    cell = run(
                        actor,
                        w.organization,
                        calls(w, views[actor, w.clinic], w.clinic, w.encounter)[guard],
                    )
                    transaction.set_rollback(True)
                narrowed += 1
                assert cell.names == names_for(
                    guard,
                    remaining,
                    w.clinic,
                    subject_enrollment(w, actor, w.encounter),
                ), (guard, role, removed)
                if any(name in remaining for name in SPEC[guard]):
                    assert cell.error is None, (guard, role, removed, cell.error)
                else:
                    assert_refused(guard, cell, reference[actor])
    assert narrowed == sum(
        len(set(SPEC[guard]) & BUNDLES_V1[role]) for guard, role in cases
    )


@pytest.mark.parametrize(
    "guard",
    [guard for guard, permissions in SPEC.items() if permissions == CLINICAL],
)
def test_clinical_guards_vary_the_subject_patient_relation(
    rbac_graph: RbacGraph, guard: str
) -> None:
    w = seed_world(rbac_graph)
    graph = w.graph
    related = member(w, "physician", care_team=w.enrollment)
    elsewhere = member(w, "physician", care_team=w.other_enrollment)
    unrelated = member(w, "physician")
    unregistered = member(w, "physician", registered=False)
    with owner_context(w.organization):
        _care_team(w, unregistered, "physician", w.enrollment)
    held = BUNDLES_V1["physician"]
    views = {
        (actor, w.clinic): view_call(w.clinic, actor)
        for actor in (related, elsewhere, unrelated, unregistered, graph.physician)
    }
    with matrix_transaction(w):
        # Permitted: care-team membership, or the open encounter's own physician.
        for actor in (related, graph.physician):
            cell = run(
                actor,
                w.organization,
                calls(w, views[actor, w.clinic], w.clinic, w.encounter)[guard],
            )
            assert cell.error is None, (actor, cell.error)
            assert cell.names == names_for(guard, held, w.clinic, w.enrollment)
            assert cell.writes > 0
        # Refused: another patient's care team, no relation, no registration.
        for actor in (elsewhere, unrelated, unregistered):
            cell = run(
                actor,
                w.organization,
                calls(w, views[actor, w.clinic], w.clinic, w.encounter)[guard],
            )
            # The decision first, then the exact names that reached it.
            assert_refused(guard, cell, b"")
            assert cell.names == names_for(guard, held, w.clinic, w.enrollment)
        # An unknown encounter carries no patient: refused identically.
        cell = run(
            related,
            w.organization,
            calls(w, views[related, w.clinic], w.clinic, uuid4())[guard],
        )
        assert cell.names == names_for(guard, held, w.clinic, None)
        assert_refused(guard, cell, b"")


def _row_counts(superuser_database_url: str) -> dict[str, int]:
    """Committed rows per clinic_app table, read outside the request's roles."""
    with psycopg.connect(superuser_database_url) as raw:
        tables = [
            str(row[0])
            for row in raw.execute(
                "SELECT c.relname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='clinic_app' AND c.relkind IN ('r','p') "
                "ORDER BY 1"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for table in tables:
            row = raw.execute(
                psql.SQL("SELECT count(*) FROM clinic_app.{}").format(
                    psql.Identifier(table)
                )
            ).fetchone()
            assert row is not None
            counts[table] = int(row[0])
    return counts


NAMED_VOLATILE = (
    # Named per SC-1: CSRF tokens and CSP nonces may differ between refusals.
    (re.compile(rb'name="csrfmiddlewaretoken" value="[^"]+"'), b"csrf"),
    (re.compile(rb'nonce="[^"]+"'), b"nonce"),
)


def _normalized(content: bytes) -> bytes:
    for pattern, name in NAMED_VOLATILE:
        content = pattern.sub(name, content)
    return content


def test_staff_page_refusals_equal_the_unknown_clinic_refusal_and_change_nothing(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    w = seed_world(rbac_graph)
    physician = member(w, "physician", care_team=w.enrollment)
    admin = member(w, "clinic_admin")
    finance = member(w, "finance")
    foreign = member(w, "clinic_admin", clinic=w.graph.clinic_b)
    encounter = str(w.encounter)
    disclosure = {"disclosure-encounter_id": encounter, "disclosure-informed": "on"}
    acknowledge = {"ack-session_id": encounter, "ack-participant_kind": "caregiver"}
    publish = {"purpose": "marketing", "text": "Sintetico recusa"}
    cases: list[tuple[str, UUID, dict[str, str] | None]] = [
        # The page gate: a role held only in another clinic.
        ("foreign-get", foreign, None),
        ("foreign-post", foreign, {"action": "publish", **publish}),
        # In-view refusals by authorised page actors.
        ("physician-publish", physician, {"action": "publish", **publish}),
        (
            "physician-notice",
            physician,
            {"action": "publish_notice", "notice-topic": "ai_use", "notice-text": "x"},
        ),
        ("admin-disclosure", admin, {"action": "disclosure", **disclosure}),
        ("admin-acknowledge", admin, {"action": "acknowledge", **acknowledge}),
        ("finance-receipts", finance, {"action": "receipts", "enrollment_id": ""}),
        (
            "physician-unknown-encounter",
            physician,
            {
                "action": "disclosure",
                **disclosure,
                "disclosure-encounter_id": str(uuid4()),
            },
        ),
        ("physician-malformed-receipts", physician, {"action": "receipts"}),
        ("physician-unknown-action", physician, {"action": "forged"}),
        ("physician-empty", physician, {}),
    ]
    for name, actor, data in cases:
        with staff_client(actor) as client:
            unknown = f"/clinics/{uuid4()}/consent/"
            # The first request after sign-in refreshes the session; compare
            # the refusals in the steady state that follows it.
            assert client.get(unknown).status_code == 403
            reference = (
                client.get(unknown) if data is None else client.post(unknown, data)
            )
            assert reference.status_code == 403, name
            session = dict(client.session.items())
            key = client.session.session_key
            before = _row_counts(superuser_database_url)
            url = f"/clinics/{w.clinic}/consent/"
            response = client.get(url) if data is None else client.post(url, data)
            assert response.status_code == 403, name
            # In-view cases: the page gate admits this actor, so the refusal
            # came from the action's own guard or input check.
            assert (client.get(url).status_code == 200) is (actor != foreign), name
            assert _normalized(response.content) == _normalized(reference.content), name
            assert response.cookies.keys() == reference.cookies.keys() == set(), name
            assert client.session.session_key == key, name
            assert dict(client.session.items()) == session, name
            assert _row_counts(superuser_database_url) == before, name
    # Control: the same surface does write and answer 200 when permitted.
    with staff_client(admin) as client:
        before = _row_counts(superuser_database_url)
        response = client.post(
            f"/clinics/{w.clinic}/consent/", {"action": "publish", **publish}
        )
        assert response.status_code == 200
        assert _row_counts(superuser_database_url) != before


# RLS read policies are the only layer on reads: prove them per table.
READ_POLICY: dict[type[Model], tuple[str, ...]] = {
    ConsentText: PAGE,
    NoticeVersion: PAGE,
    ConsentAcceptance: READ,
    ConsentRevocation: READ,
    RefusalRecord: READ,
    ParticipantAcknowledgment: READ,
    AIUseDisclosure: READ,
}


def _seed_every_consent_row(w: World) -> None:
    graph = w.graph
    session, _ = _patient_session(graph)
    with runtime_role(), tenant_context(graph.clinic_admin, w.organization):
        consent.publish_notice(clinic_id=w.clinic, topic="ai_use", text="Sintetico")
        refused = consent.publish_text(
            clinic_id=w.clinic, purpose="marketing", text="Sintetico marketing"
        )
    with runtime_role(), patient_session_context(session):
        receipt = accept_offered(consent.available_texts()[-1])
        consent.revoke_consent(acceptance_id=receipt.pk)
        _, offer = consent.prepare_acceptance(text_id=refused.pk)
        consent.record_refusal(offer=offer, purpose="marketing")
    with runtime_role(), tenant_context(graph.physician, w.organization):
        consent.record_ai_disclosure(
            clinic_id=w.clinic, encounter_id=w.encounter, informed=True, refused=True
        )
        consent.acknowledge_participant(
            clinic_id=w.clinic, session_id=w.encounter, participant_kind="caregiver"
        )


def _visible(w: World, actor: UUID) -> dict[str, bool]:
    _bind(actor, w.organization)
    return {
        model.__name__: model._default_manager.filter(clinic_id=w.clinic).exists()
        for model in READ_POLICY
    }


def test_rls_read_policies_follow_the_bundles_for_every_role(
    rbac_graph: RbacGraph,
) -> None:
    w = seed_world(rbac_graph)
    _seed_every_consent_row(w)
    actors = {role: member(w, role, care_team=w.enrollment) for role in ROLES}
    foreign = member(w, "clinic_admin", clinic=w.graph.clinic_b)
    with owner_context(w.organization):
        for model in READ_POLICY:
            assert model._default_manager.filter(clinic_id=w.clinic).exists(), model
    with matrix_transaction(w):
        for role, actor in actors.items():
            assert _visible(w, actor) == {
                model.__name__: role in permitted(names)
                for model, names in READ_POLICY.items()
            }, role
            # Narrow every read name the role holds: nothing stays visible.
            with transaction.atomic():
                with as_owner():
                    for name in set(PAGE) & BUNDLES_V1[role]:
                        RoleGrant.objects.create(
                            organization_id=w.organization,
                            clinic_id=w.clinic,
                            role=role,
                            permission=name,
                            valid_from=timezone.now() - timedelta(days=1),
                        )
                assert not any(_visible(w, actor).values()), role
                transaction.set_rollback(True)
            with transaction.atomic():
                with as_owner():
                    User.objects.filter(pk=actor).update(is_active=False)
                assert not any(_visible(w, actor).values()), role
                transaction.set_rollback(True)
        assert not any(_visible(w, foreign).values())


def _direct_attestations(w: World, actor: UUID, patient: UUID) -> dict[str, str | None]:
    """INSERT both attestation rows as clinic_app, bypassing the service."""
    outcome: dict[str, str | None] = {}
    rows: dict[str, Callable[[], object]] = {
        "disclosure": lambda: AIUseDisclosure.objects.create(
            organization_id=w.organization,
            clinic_id=w.clinic,
            encounter_id=w.encounter,
            patient_id=patient,
            informed=True,
            refused=False,
            recorded_by_id=actor,
            recorded_at=timezone.now(),
        ),
        "participant": lambda: ParticipantAcknowledgment.objects.create(
            organization_id=w.organization,
            clinic_id=w.clinic,
            session_id=w.encounter,
            participant_kind="companion",
            acknowledged_by_clinician_id=actor,
            acknowledged_at=timezone.now(),
        ),
    }
    for name, insert in rows.items():
        _bind(actor, w.organization)
        try:
            with transaction.atomic():
                insert()
                transaction.set_rollback(True)
        except DatabaseError as error:
            cause = error.__cause__
            assert isinstance(cause, psycopg.Error)
            outcome[name] = f"{cause.sqlstate}:{cause.diag.message_primary}"
        else:
            outcome[name] = None
    return outcome


def test_database_attestation_guard_derives_the_relation_itself(
    rbac_graph: RbacGraph,
) -> None:
    """consent_guard refuses on its own, not only behind the Python guard."""
    w = seed_world(rbac_graph)
    related = member(w, "physician", care_team=w.enrollment)
    elsewhere = member(w, "physician", care_team=w.other_enrollment)
    unrelated = member(w, "physician")
    nurse = member(w, "nurse", care_team=w.enrollment)
    refused = "42501:consent staff authority required"
    with owner_context(w.organization):
        patient = Encounter.objects.get(pk=w.encounter).patient_id
    with matrix_transaction(w):
        for actor in (related, w.graph.physician):
            assert _direct_attestations(w, actor, patient) == dict.fromkeys(
                ("disclosure", "participant")
            )
        # The trigger decides first; an RLS refusal instead would mean the
        # trigger's own permission or relation check is gone.
        for actor in (elsewhere, unrelated, nurse):
            assert _direct_attestations(w, actor, patient) == dict.fromkeys(
                ("disclosure", "participant"), refused
            )
