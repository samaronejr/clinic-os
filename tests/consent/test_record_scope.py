"""Record scope: every clinic/organization/patient binding decides both ways.

Sites are derived, never listed from memory. On the Python side they are every
ORM call anywhere in ``apps/consent`` (functions, methods, nested scopes) whose
keyword or ``defaults`` key names a scope relation or column (``clinic``,
``clinic_id``, ``enrollment__clinic_id``) or whose value reads one, plus every
comparison that reads one. On the SQL side they are every comparison involving
a scope column in the consent SQL functions and in the RLS policy expressions
of every consent table, read from the live catalog. Derivation fails closed:
every scope token in the Python modules must sit in a derived site or in a
named non-binding context (anything else, e.g. a ``Q`` object, a positional
filter argument or raw SQL, fails), a ``**`` splat or a non-literal
``defaults`` fails, and every SQL scope token must sit in a parsed comparison
or a named non-binding context. Each site maps to the cell that decides it or
to an equivalence with the binding that makes it redundant, and the tables
must equal the derivation.

The world has one patient enrolled in clinics A and B of the same
organization, plus a second patient. Staff hold the permission in both
clinics, so row-level security cannot hide a missing Python scope filter.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.audit.services import record_phase1_event, verify_chain
from apps.consent import services as consent
from apps.consent.models import (
    AIUseDisclosure,
    ConsentAcceptance,
    ConsentRevocation,
    ConsentText,
    NoticeVersion,
    ParticipantAcknowledgment,
    RefusalRecord,
)
from apps.ehr.services import open_encounter
from apps.identity import current_context
from apps.identity.current_context import CurrentActorError
from apps.identity.models import (
    CareTeamMembership,
    Clinic,
    ProfessionalRegistration,
    User,
    UserClinicRole,
)
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.intake.services import create_patient
from apps.tenancy.db import tenant_context
from django.apps import apps
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from consent.test_authority import run
from identity.legacy_sql_inventory import migration_definitions
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_cross_clinic_appointment_setups,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[2]
SCOPE = ("clinic_id", "organization_id", "patient_id")
# Lookup parts naming a scope relation or column; any one makes a keyword,
# name or attribute a scope token (``clinic``, ``enrollment__clinic_id``).
SCOPE_PARTS = frozenset({"clinic", "organization", "patient", *SCOPE})
ORM_METHODS = frozenset(
    {
        "filter",
        "get",
        "exclude",
        "get_or_create",
        "create",
        "update",
        "update_or_create",
    }
)

# Python site -> the cell deciding it, or ("equivalent", why) when another
# binding decides it first.
PY_SITES: dict[str, str | tuple[str, str]] = {
    "publish_text:Clinic.get:pk": "test_writes_store_the_exact_scope",
    "publish_text:ConsentText.create:organization_id": (
        "test_writes_store_the_exact_scope"
    ),
    "publish_text:ConsentText.create:clinic_id": "test_writes_store_the_exact_scope",
    "publish_text:ConsentText.filter:clinic_id": "test_writes_store_the_exact_scope",
    "publish_notice:Clinic.get:pk": "test_writes_store_the_exact_scope",
    "publish_notice:NoticeVersion.create:organization_id": (
        "test_writes_store_the_exact_scope"
    ),
    "publish_notice:NoticeVersion.create:clinic_id": (
        "test_writes_store_the_exact_scope"
    ),
    "publish_notice:NoticeVersion.filter:clinic_id": (
        "test_writes_store_the_exact_scope"
    ),
    "available_texts:ConsentText.filter:clinic_id": (
        "equivalent",
        "consent_text_read's patient branch binds the session clinic; "
        "test_patient_sessions_see_only_their_clinic pins the combined result",
    ),
    "available_notices:NoticeVersion.filter:clinic_id": (
        "equivalent",
        "consent_notice_read's patient branch binds the session clinic; "
        "test_patient_sessions_see_only_their_clinic pins the combined result",
    ),
    "_resolve_offer:ConsentText.filter:clinic_id": (
        "equivalent",
        "runs in the patient context, where consent_text_read's patient branch "
        "binds the session clinic; test_patient_sessions_see_only_their_clinic "
        "pins that clinic B resolves B's version while A holds a newer one",
    ),
    "record_consent:ConsentAcceptance.get_or_create:defaults.organization_id": (
        "test_writes_store_the_exact_scope"
    ),
    "record_consent:ConsentAcceptance.get_or_create:defaults.clinic_id": (
        "test_writes_store_the_exact_scope"
    ),
    "record_consent:ConsentAcceptance.get_or_create:defaults.patient_id": (
        "test_writes_store_the_exact_scope"
    ),
    "record_refusal:RefusalRecord.get_or_create:defaults.organization_id": (
        "test_writes_store_the_exact_scope"
    ),
    "record_refusal:RefusalRecord.get_or_create:defaults.clinic_id": (
        "test_writes_store_the_exact_scope"
    ),
    "record_refusal:RefusalRecord.get_or_create:defaults.patient_id": (
        "test_writes_store_the_exact_scope"
    ),
    "revoke_consent:ConsentRevocation.get_or_create:defaults.organization_id": (
        "test_writes_store_the_exact_scope"
    ),
    "revoke_consent:ConsentRevocation.get_or_create:defaults.clinic_id": (
        "test_writes_store_the_exact_scope"
    ),
    "staff_receipts:_receipts.filter:clinic_id": (
        "test_staff_reads_return_exactly_the_named_clinics_records"
    ),
    "staff_refusals:RefusalRecord.filter:clinic_id": (
        "test_staff_reads_return_exactly_the_named_clinics_records"
    ),
    "consent_for_future_use:ConsentText.filter:clinic_id": (
        "test_staff_reads_return_exactly_the_named_clinics_records"
    ),
    "consent_for_future_use:ConsentAcceptance.filter:clinic_id": (
        "equivalent",
        "the acceptance must reference the clinic's current text, and "
        "consent_guard binds an acceptance's clinic to its text's clinic "
        "(t.clinic_id/NEW.clinic_id vs the session, cells in "
        "test_trigger_refuses_every_crossed_binding)",
    ),
    "_encounter_enrollment:PatientClinicEnrollment.filter:organization_id": (
        "equivalent",
        "a clinic belongs to exactly one organization (identity_clinic FK), "
        "so the clinic_id binding already fixes it",
    ),
    "_encounter_enrollment:PatientClinicEnrollment.filter:clinic_id": (
        "test_attestations_bind_the_encounter_patients_enrollment"
    ),
    "_encounter_enrollment:PatientClinicEnrollment.filter:patient_id": (
        "test_attestations_bind_the_encounter_patients_enrollment"
    ),
    "record_ai_disclosure:AIUseDisclosure.get_or_create:defaults.organization_id": (
        "test_writes_store_the_exact_scope"
    ),
    "record_ai_disclosure:AIUseDisclosure.get_or_create:defaults.clinic_id": (
        "test_writes_store_the_exact_scope"
    ),
    "record_ai_disclosure:AIUseDisclosure.get_or_create:defaults.patient_id": (
        "test_writes_store_the_exact_scope"
    ),
    "record_ai_disclosure:Encounter.filter:clinic_id": (
        "test_attestations_bind_the_encounter_patients_enrollment"
    ),
    "ai_disclosure_status:AIUseDisclosure.filter:clinic_id": (
        "test_staff_reads_return_exactly_the_named_clinics_records"
    ),
    "acknowledge_participant:ParticipantAcknowledgment.get_or_create:"
    "defaults.organization_id": "test_writes_store_the_exact_scope",
    "acknowledge_participant:ParticipantAcknowledgment.get_or_create:"
    "defaults.clinic_id": "test_writes_store_the_exact_scope",
    "acknowledge_participant:Encounter.filter:clinic_id": (
        "test_attestations_bind_the_encounter_patients_enrollment"
    ),
}
# Scope tokens outside ORM sites and comparisons, keyed module:scope:context,
# each with its reason. A token in any other context fails the census, so a
# Q object, a positional filter argument, raw SQL or a new assignment of a
# scope value has to be classified before it can land.
_DECLARATION = "model field or constraint declaration (schema, not a row read)"
_ORGANIZATION_OF_CLINIC = (
    "organization_id read from the named clinic row (the Clinic.get:pk site)"
)
PY_CONTEXTS: dict[str, str] = {
    **{
        f"models:{model}:statement:Assign": _DECLARATION
        for model in (
            "AIUseDisclosure",
            "ConsentAcceptance",
            "ConsentRevocation",
            "ConsentText",
            "NoticeVersion",
            "ParticipantAcknowledgment",
            "RefusalRecord",
        )
    },
    "models:ConsentText.Meta:call:UniqueConstraint": _DECLARATION,
    "models:NoticeVersion.Meta:call:UniqueConstraint": _DECLARATION,
    "services:ConsentAuthority:statement:AnnAssign": (
        "dataclass fields filled only from consent_session()"
    ),
    "services:publish_text:statement:Assign": _ORGANIZATION_OF_CLINIC,
    "services:publish_text:call:values_list": _ORGANIZATION_OF_CLINIC,
    "services:publish_notice:statement:Assign": _ORGANIZATION_OF_CLINIC,
    "services:publish_notice:call:values_list": _ORGANIZATION_OF_CLINIC,
    "services:_lock:call:execute": "advisory-lock key",
    "views:staff_consent:statement:AnnAssign": (
        "template context for the clinic form URL"
    ),
    "views:_staff_action:call:handler": (
        "dispatch to the views helpers of the same dict, each forwarding the "
        "URL clinic to a consent service whose own sites are derived"
    ),
}
# Callees whose scope argument is not a row binding, wherever they are called.
# Calls to any function defined in apps/consent are forwards: the callee's own
# tokens are derived here.
CALLEE_ROLES = {
    "require_permission": "authority argument (test_authority decides it)",
    "record_phase1_event": "audit event scope",
    "build_phase1_audit_event": "audit event scope",
    "AuditTrustedContext": "audit chain scope",
    "reverse": "URL argument",
    "path": "URL pattern",
}

_ENROLLMENT_ORG = (
    "equivalent",
    "a clinic belongs to exactly one organization, so en.clinic_id=e.clinic_id "
    "already fixes the enrollment's organization",
)
_ENCOUNTER_CLINIC = (
    "equivalent",
    "has_permission('clinical.write', NEW.clinic_id, enrollment) refuses an "
    "enrollment of another clinic first (42501); "
    "test_trigger_refuses_every_crossed_binding pins that refusal",
)
_REVOCATION_BY_ENROLLMENT = (
    "equivalent",
    "a.enrollment_id IS DISTINCT FROM s.enrollment_id refuses first and an "
    "enrollment fixes both clinic and patient; "
    "test_trigger_refuses_every_crossed_binding pins that refusal",
)
_TRIGGER = "test_trigger_refuses_every_crossed_binding"
# SQL site: function:comparison#occurrence (order within the function body).
SQL_SITES: dict[str, str | tuple[str, str]] = {
    # consent_consenttext / consent_noticeversion publication branches
    "consent_guard:c.id = NEW.clinic_id#0": _TRIGGER,
    "consent_guard:c.organization_id = NEW.organization_id#0": _TRIGGER,
    "consent_guard:clinic_id = NEW.clinic_id#0": "test_writes_store_the_exact_scope",
    "consent_guard:c.id = NEW.clinic_id#1": _TRIGGER,
    "consent_guard:c.organization_id = NEW.organization_id#1": _TRIGGER,
    "consent_guard:clinic_id = NEW.clinic_id#1": "test_writes_store_the_exact_scope",
    # participant acknowledgment branch
    "consent_guard:en.organization_id = e.organization_id#0": _ENROLLMENT_ORG,
    "consent_guard:en.clinic_id = e.clinic_id#0": (
        "test_attestations_bind_the_encounter_patients_enrollment"
    ),
    "consent_guard:en.patient_id = e.patient_id#0": (
        "test_attestations_bind_the_encounter_patients_enrollment"
    ),
    "consent_guard:e.clinic_id IS DISTINCT FROM NEW.clinic_id#0": _ENCOUNTER_CLINIC,
    "consent_guard:e.organization_id IS DISTINCT FROM NEW.organization_id#0": (
        _TRIGGER
    ),
    # AI-use disclosure branch
    "consent_guard:en.organization_id = e.organization_id#1": _ENROLLMENT_ORG,
    "consent_guard:en.clinic_id = e.clinic_id#1": (
        "test_attestations_bind_the_encounter_patients_enrollment"
    ),
    "consent_guard:en.patient_id = e.patient_id#1": (
        "test_attestations_bind_the_encounter_patients_enrollment"
    ),
    "consent_guard:e.clinic_id IS DISTINCT FROM NEW.clinic_id#1": _ENCOUNTER_CLINIC,
    "consent_guard:e.organization_id IS DISTINCT FROM NEW.organization_id#1": (
        _TRIGGER
    ),
    "consent_guard:e.patient_id IS DISTINCT FROM NEW.patient_id#0": _TRIGGER,
    # patient-session binding shared by acceptance, refusal and revocation
    "consent_guard:NEW.organization_id IS DISTINCT FROM s.organization_id#0": (
        _TRIGGER
    ),
    "consent_guard:NEW.clinic_id IS DISTINCT FROM s.clinic_id#0": _TRIGGER,
    # acceptance branch
    "consent_guard:t.clinic_id IS DISTINCT FROM s.clinic_id#0": _TRIGGER,
    "consent_guard:NEW.patient_id IS DISTINCT FROM s.patient_id#0": _TRIGGER,
    "consent_guard:newer.clinic_id = t.clinic_id#0": (
        "test_patient_sessions_see_only_their_clinic"
    ),
    # refusal branch
    "consent_guard:t.clinic_id IS DISTINCT FROM s.clinic_id#1": _TRIGGER,
    "consent_guard:NEW.patient_id IS DISTINCT FROM s.patient_id#1": _TRIGGER,
    "consent_guard:newer.clinic_id = t.clinic_id#1": (
        "test_patient_sessions_see_only_their_clinic"
    ),
    # revocation branch
    "consent_guard:a.patient_id IS DISTINCT FROM s.patient_id#0": (
        _REVOCATION_BY_ENROLLMENT
    ),
    "consent_guard:a.clinic_id IS DISTINCT FROM s.clinic_id#0": (
        _REVOCATION_BY_ENROLLMENT
    ),
    # consent_audit: the tenant chain's previous hash
    "consent_audit:a.organization_id = s.organization_id#0": (
        "test_consent_audit_chains_within_its_organization"
    ),
}
# RLS policy sites: table.policy.(using|check):comparison#occurrence.
_RLS = "test_rls_reads_bind_the_patient_session_and_tenant"
_TENANT = "organization_id = current_setting('app.current_tenant')#0"
_ATTESTATION = "test_attestations_bind_the_encounter_patients_enrollment"
SQL_SITES |= {
    **{
        f"{table}.setup_tenant.using:{_TENANT}": _RLS
        for table in (
            "consent_aiusedisclosure",
            "consent_consentacceptance",
            "consent_consentrevocation",
            "consent_consenttext",
            "consent_noticeversion",
            "consent_participantacknowledgment",
            "consent_refusalrecord",
        )
    },
    # Patient-bound rows: consent_guard binds the session, not the tenant, so
    # the owner-role WITH CHECK decides by itself.
    **{
        f"{table}.setup_tenant.check:{_TENANT}": "test_owner_inserts_bind_the_tenant"
        for table in (
            "consent_consentacceptance",
            "consent_consentrevocation",
            "consent_refusalrecord",
        )
    },
    **{
        f"{table}.setup_tenant.check:{_TENANT}": (
            "equivalent",
            "the BEFORE INSERT consent_guard checks has_permission, which binds "
            "app.current_tenant, and refuses first (42501); "
            "test_owner_inserts_bind_the_tenant pins that refusal",
        )
        for table in (
            "consent_aiusedisclosure",
            "consent_consenttext",
            "consent_noticeversion",
            "consent_participantacknowledgment",
        )
    },
    "consent_consenttext.consent_text_read.using:"
    "clinic_id = (SELECT s.clinic_id)#0": _RLS,
    "consent_noticeversion.consent_notice_read.using:"
    "clinic_id = (SELECT s.clinic_id)#0": _RLS,
    "consent_aiusedisclosure.consent_ai_disclosure_read.using:"
    "consent_aiusedisclosure.patient_id = s.patient_id#0": _RLS,
    "consent_aiusedisclosure.consent_ai_disclosure_read.using:"
    "consent_aiusedisclosure.clinic_id = s.clinic_id#0": _RLS,
    **{
        f"{table}.{policy}.check:{comparison}#0": decision
        for table, policy in (
            ("consent_aiusedisclosure", "consent_ai_disclosure_insert"),
            ("consent_participantacknowledgment", "consent_participant_insert"),
        )
        for comparison, decision in (
            ("en.organization_id = e.organization_id", _ENROLLMENT_ORG),
            ("en.clinic_id = e.clinic_id", _ATTESTATION),
            ("en.patient_id = e.patient_id", _ATTESTATION),
        )
    },
}
# Scope tokens that are not bindings between rows, each with its reason. Any
# other occurrence outside a parsed comparison fails the census.
NON_BINDING = {
    r"has_permission\('[a-z_.]+',\s*NEW\.clinic_id": "authority argument",
    r"'consent:'\|\|(?:NEW|t)\.clinic_id": "advisory-lock key",
    r"SELECT s\.id,s\.organization_id,s\.clinic_id,s\.patient_id": "session projection",
    r"SELECT s\.session_id,s\.organization_id,s\.clinic_id": "audit scope projection",
    r"'clinic-audit:'\|\|s\.organization_id": "audit chain lock key",
    r"audit_event \(organization_id": "audit insert column list",
    r"VALUES \(s\.organization_id": "audit insert value (from the scope row)",
    r"'clinic_id',s\.clinic_id": "audit payload key",
    r"has_permission\('[a-z_.]+'::text, clinic_id\b": "authority argument",
    r"consent_session\(\) s\(session_id, organization_id, clinic_id, patient_id, "
    r"enrollment_id\)": "consent_session() column alias list",
}
# The right side is an identifier, a scalar subquery's projection or a GUC.
COMPARISON = re.compile(
    r"(?P<left>[A-Za-z_][\w.]*)\s*(?P<op>IS NOT DISTINCT FROM|IS DISTINCT FROM|<>|=)"
    r"\s*(?:(?P<right>[A-Za-z_][\w.]*)"
    r"|\(\s*SELECT\s+(?P<select>[A-Za-z_][\w.]*)"
    r"|\(\s*NULLIF\(current_setting\('(?P<setting>[\w.]+)')"
)
TOKEN = re.compile(r"\b(?:\w+\.)?(?:clinic_id|organization_id|patient_id)\b")


def _scope_key(key: str) -> bool:
    return any(part in SCOPE_PARTS for part in key.split("__"))


def _is_scope_token(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in SCOPE_PARTS
    if isinstance(node, ast.Attribute):
        return node.attr in SCOPE_PARTS
    if isinstance(node, ast.keyword):
        return node.arg is not None and _scope_key(node.arg)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # A lookup string or text naming a scope column, e.g. raw SQL.
        return _scope_key(node.value) or TOKEN.search(node.value) is not None
    return False


def _callee(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ast.unparse(call.func)


def _ancestors(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    chain = []
    while node in parents:
        node = parents[node]
        chain.append(node)
    return chain


def _qualname(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    names = [
        item.name
        for item in reversed(_ancestors(node, parents))
        if isinstance(item, scopes)
    ]
    return ".".join(names) or "<module>"


def _context(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> tuple[str, str]:
    """The innermost call (callee) or statement holding a scope token."""
    for item in _ancestors(node, parents):
        if isinstance(item, ast.Call):
            return "call", _callee(item)
        if isinstance(item, ast.stmt):
            return "statement", type(item).__name__
    return "statement", "Module"


def _receiver(call: ast.Call) -> str:
    node: ast.AST = call.func
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Name):
            return node.id
        else:
            pytest.fail(f"unresolved ORM receiver {ast.unparse(call)}")


def _orm_sites(
    call: ast.Call, parents: dict[ast.AST, ast.AST]
) -> list[tuple[str, list[ast.AST]]]:
    """Scope sites of one ORM call and the nodes each site covers."""
    pairs: list[tuple[str, ast.AST, ast.expr]] = []
    for keyword in call.keywords:
        # Fail closed: a splat or computed defaults hides bindings.
        assert keyword.arg is not None, ast.unparse(call)
        if keyword.arg == "defaults":
            assert isinstance(keyword.value, ast.Dict), ast.unparse(call)
            for key, value in zip(
                keyword.value.keys, keyword.value.values, strict=True
            ):
                assert isinstance(key, ast.Constant), ast.unparse(call)
                assert isinstance(key.value, str), ast.unparse(call)
                pairs.append((f"defaults.{key.value}", key, value))
        else:
            pairs.append((keyword.arg, keyword, keyword.value))
    assert isinstance(call.func, ast.Attribute)
    return [
        (
            f"{_qualname(call, parents)}:{_receiver(call)}.{call.func.attr}:{key}",
            [item for top in (node, value) for item in ast.walk(top)],
        )
        for key, node, value in pairs
        if _scope_key(key.removeprefix("defaults."))
        or any(_is_scope_token(item) for item in ast.walk(value))
    ]


def python_census() -> tuple[set[str], set[str]]:
    """Derived Python sites, and the contexts of every other scope token."""
    modules = {
        path: ast.parse(path.read_text())
        for path in sorted((ROOT / "apps/consent").glob("*.py"))
    }
    forwards = {
        node.name
        for tree in modules.values()
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    sites: list[str] = []
    contexts: set[str] = set()
    for path, tree in modules.items():
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        covered: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ORM_METHODS
            ):
                for site, nodes in _orm_sites(node, parents):
                    sites.append(site)
                    covered.update(map(id, nodes))
            elif isinstance(node, ast.Compare) and any(
                _is_scope_token(item) for item in ast.walk(node)
            ):
                sites.append(f"{_qualname(node, parents)}:compare:{ast.unparse(node)}")
                covered.update(map(id, ast.walk(node)))
        for node in ast.walk(tree):
            if not _is_scope_token(node) or id(node) in covered:
                continue
            kind, name = _context(node, parents)
            if kind == "call" and (name in CALLEE_ROLES or name in forwards):
                continue
            contexts.add(f"{path.stem}:{_qualname(node, parents)}:{kind}:{name}")
    assert len(sites) == len(set(sites)), "ambiguous site id"
    return set(sites), contexts


def consent_functions() -> dict[str, str]:
    names = sorted(
        name.split(".", 1)[1]
        for name, paths in migration_definitions().items()
        if any(path.startswith("apps/consent/") for path in paths)
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proname, prosrc FROM pg_catalog.pg_proc "
            "WHERE pronamespace='clinic_app'::regnamespace AND proname = ANY(%s)",
            [names],
        )
        bodies = {str(row[0]): str(row[1]) for row in cursor.fetchall()}
    assert sorted(bodies) == names  # every source definition is deployed once
    return bodies


def consent_policies() -> dict[str, str]:
    """Every RLS policy expression on every consent table, from the catalog."""
    tables = sorted(
        model._meta.db_table for model in apps.get_app_config("consent").get_models()
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname, c.relrowsecurity AND c.relforcerowsecurity, "
            "p.polname, pg_catalog.pg_get_expr(p.polqual, p.polrelid), "
            "pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) "
            "FROM pg_catalog.pg_class c "
            "LEFT JOIN pg_catalog.pg_policy p ON p.polrelid = c.oid "
            "WHERE c.relnamespace = 'clinic_app'::regnamespace "
            "AND c.relname = ANY(%s)",
            [tables],
        )
        rows = cursor.fetchall()
    # Every consent table exists, forces RLS and carries policies.
    assert sorted({row[0] for row in rows}) == tables
    assert all(row[1] and row[2] for row in rows), rows
    expressions: dict[str, str] = {}
    for table, _, policy, using, check in rows:
        for part, expression in (("using", using), ("check", check)):
            if expression is not None:
                expressions[f"{table}.{policy}.{part}"] = str(expression)
    return expressions


def _right(match: re.Match[str]) -> str:
    if match["right"]:
        return match["right"]
    if match["select"]:
        return f"(SELECT {match['select']})"
    return f"current_setting('{match['setting']}')"


def sql_sites(bodies: dict[str, str]) -> set[str]:
    sites: set[str] = set()
    for name, body in bodies.items():
        covered: list[tuple[int, int]] = []
        seen: dict[str, int] = {}
        for match in COMPARISON.finditer(body):
            sides = (match["left"], match["right"] or match["select"] or "")
            if not any(side.rsplit(".", 1)[-1] in SCOPE for side in sides):
                continue
            text = f"{match['left']} {match['op']} {_right(match)}"
            index = seen.get(text, 0)
            seen[text] = index + 1
            sites.add(f"{name}:{text}#{index}")
            covered.append(match.span())
        covered.extend(
            found.span()
            for pattern in NON_BINDING
            for found in re.finditer(pattern, body)
        )
        for token in TOKEN.finditer(body):
            assert any(
                start <= token.start() and token.end() <= end for start, end in covered
            ), (
                name,
                "unclassified scope token",
                body[token.start() - 40 : token.end()],
            )
    return sites


def test_record_scope_sites_are_derived_and_every_site_is_decided() -> None:
    sites, contexts = python_census()
    assert sites == set(PY_SITES)
    assert contexts == set(PY_CONTEXTS)
    assert all(PY_CONTEXTS.values())
    assert all(CALLEE_ROLES.values())
    assert sql_sites({**consent_functions(), **consent_policies()}) == set(SQL_SITES)
    cells = {name for name in globals() if name.startswith("test_")}
    for decision in (*PY_SITES.values(), *SQL_SITES.values()):
        if isinstance(decision, tuple):
            assert decision[0] == "equivalent", decision
            assert decision[1], decision
        else:
            assert decision in cells, decision


def test_derivation_fails_closed_on_hidden_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "apps/consent/leaky.py"
    module.parent.mkdir(parents=True)
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    for source in (
        "def leak(kw):\n    Model.objects.filter(**kw)\n",
        "def leak(d):\n    Model.objects.get_or_create(pk=1, defaults=d)\n",
    ):
        module.write_text(source)
        with pytest.raises(AssertionError):
            python_census()
    module.write_text("def read(clinic_id):\n    Model.objects.filter(pk=clinic_id)\n")
    assert python_census() == ({"read:Model.filter:pk"}, set())
    # Every other spelling of a binding is a derived site.
    derived = {
        "class Repo:\n    def read(self, c):\n"
        "        Model.objects.get(clinic_id=c)\n": ("Repo.read:Model.get:clinic_id"),
        "async def read(c):\n    Model.objects.exclude(clinic=c)\n": (
            "read:Model.exclude:clinic"
        ),
        "def read(c):\n    Model.objects.filter(enrollment__patient_id=c)\n": (
            "read:Model.filter:enrollment__patient_id"
        ),
        "def read(row, c):\n    if row.organization_id != c:\n        raise E\n": (
            "read:compare:row.organization_id != c"
        ),
    }
    for source, site in derived.items():
        module.write_text(source)
        assert python_census() == ({site}, set()), source
    # A token the derivation cannot bind to a site lands in an unclassified
    # context, which the census rejects.
    hidden = {
        "def leak(c):\n    Model.objects.filter(Q(clinic_id=c))\n": "call:Q",
        "def leak(c):\n    Model.objects.filter(Exists(c.clinic))\n": "call:Exists",
        "def leak(cursor, c):\n"
        "    cursor.execute('SELECT 1 FROM t WHERE clinic_id=%s', [c])\n": (
            "call:execute"
        ),
        "def leak(row):\n    clinic = row.clinic\n": "statement:Assign",
    }
    for source, context in hidden.items():
        module.write_text(source)
        sites, contexts = python_census()
        assert sites == set(), source
        assert contexts == {f"leaky:leak:{context}"}, source
        assert not contexts <= set(PY_CONTEXTS)
    with pytest.raises(AssertionError, match="unclassified scope token"):
        sql_sites({"planted": "SELECT 1 WHERE coalesce(x.clinic_id, y) IS NULL"})
    with pytest.raises(AssertionError, match="unclassified scope token"):
        sql_sites({"planted": "(x.clinic_id IN ( SELECT s.clinic_id FROM t s))"})
    assert sql_sites(
        {
            "planted": "SELECT 1 WHERE a.clinic_id = b.clinic_id",
            "t.p.using": "((organization_id = (NULLIF(current_setting('app.t'::text, "
            "true), ''::text))::uuid) AND (clinic_id = ( SELECT s.clinic_id\n"
            "   FROM clinic_app.consent_session() s(session_id, organization_id, "
            "clinic_id, patient_id, enrollment_id))))",
        }
    ) == {
        "planted:a.clinic_id = b.clinic_id#0",
        "t.p.using:organization_id = current_setting('app.t')#0",
        "t.p.using:clinic_id = (SELECT s.clinic_id)#0",
    }


# ---------------------------------------------------------------------------
# The two-clinic world.


@dataclass(frozen=True)
class ScopeWorld:
    graph: RbacGraph
    reader: UUID  # receptionist in clinics A and B
    patient: UUID  # P, enrolled in A and B
    other_patient: UUID  # Q, enrolled in A and B
    enrollment_a: UUID
    enrollment_b: UUID
    other_a: UUID
    other_b: UUID
    session_a: UUID
    session_b: UUID
    other_session_b: UUID
    text_a_v1: UUID  # A teleconsultation, version 1
    text_a: UUID  # A teleconsultation, version 2
    text_b: UUID  # B teleconsultation, version 1
    marketing_a: UUID
    marketing_b: UUID
    notice_a: UUID
    notice_b: UUID
    acceptance_a: UUID  # revoked
    revocation_a: UUID
    acceptance_b: UUID
    refusal_a: UUID
    refusal_b: UUID
    other_refusal_b: UUID
    encounter_a: UUID
    other_encounter_a: UUID
    encounter_b: UUID
    disclosure_a: UUID
    disclosure_b: UUID
    acknowledgment_a: UUID
    acknowledgment_b: UUID

    @property
    def clinic_a(self) -> UUID:
        return self.graph.clinic_a

    @property
    def clinic_b(self) -> UUID:
        return self.graph.clinic_b

    @property
    def organization(self) -> UUID:
        return self.graph.organization_a


def _register(
    graph: RbacGraph, user: UUID, clinic: UUID, *, care: tuple[UUID, ...] = ()
) -> None:
    now = timezone.now()
    ProfessionalRegistration.objects.create(
        organization_id=graph.organization_a,
        clinic_id=clinic,
        user_id=user,
        role="physician",
        council="CRM",
        number="SINTETICO-020",
        jurisdiction="SP" if clinic == graph.clinic_a else "RJ",
        specialty="Sintetico",
        status="regular",
        valid_from=now - timedelta(days=1),
        valid_to=now + timedelta(days=1),
    )
    for enrollment in care:
        CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            patient_enrollment_id=enrollment,
            user_id=user,
            role="physician",
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=1),
        )


def physician(graph: RbacGraph, grants: dict[UUID, tuple[UUID, ...]]) -> UUID:
    """A registered physician per clinic with care-team rows per enrollment."""
    user = User.objects.create(username=f"synthetic-scope-{uuid4().hex}").pk
    with owner_context(graph.organization_a):
        for clinic, care in grants.items():
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=clinic,
                user_id=user,
                role="physician",
            )
            _register(graph, user, clinic, care=care)
    return user


def _session(clinic: UUID, enrollment: UUID, reader: UUID, organization: UUID) -> UUID:
    with runtime_role(), tenant_context(reader, organization):
        invitation = issue_invitation(clinic_id=clinic, enrollment_id=enrollment)
    with runtime_role():
        session = redeem_invitation(clinic, invitation.secret)
    assert session is not None
    return session


def _publish(graph: RbacGraph, clinic: UUID, purpose: str, text: str) -> UUID:
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        return consent.publish_text(clinic_id=clinic, purpose=purpose, text=text).pk


def _accept(session: UUID, text: UUID, purpose: str) -> UUID:
    with runtime_role(), patient_session_context(session):
        _, offer = consent.prepare_acceptance(text_id=text)
        return consent.record_consent(offer=offer, purpose=purpose, accepted=True).pk


def _refuse(session: UUID, text: UUID, purpose: str) -> UUID:
    with runtime_role(), patient_session_context(session):
        _, offer = consent.prepare_acceptance(text_id=text)
        return consent.record_refusal(offer=offer, purpose=purpose).pk


def seed_scope_world(graph: RbacGraph) -> ScopeWorld:
    setup_a, setup_b = seed_cross_clinic_appointment_setups(graph)
    reader = graph.shared_user
    with owner_context(graph.organization_a):
        # The publisher administers both clinics; clinic_admin is also the
        # clinic B physician in this fixture.
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="clinic_admin",
        )
        _register(graph, graph.physician, graph.clinic_a)
        _register(graph, graph.clinic_admin, graph.clinic_b)
    with runtime_role(), tenant_context(reader, graph.organization_a):
        other = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Sintetico Escopo Outro",
            birth_date=date(1992, 3, 4),
            idempotency_key=uuid4(),
        )
        other_b = PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            patient_id=other.patient.pk,
            idempotency_key=uuid4(),
            create_fingerprint=b"q" * 32,
        ).pk
        appointment_a = create_synthetic_appointment(setup_a)
        other_appointment_a = create_synthetic_appointment(
            replace(
                setup_a, enrollment_id=other.enrollment.pk, patient_id=other.patient.pk
            ),
            start_local="2035-06-02T10:00",
            end_local="2035-06-02T10:30",
        )
        appointment_b = create_synthetic_appointment(
            setup_b, start_local="2035-06-02T11:00", end_local="2035-06-02T11:30"
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter_a = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment_a.pk
        ).pk
        other_encounter_a = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=other_appointment_a.pk
        ).pk
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        encounter_b = open_encounter(
            clinic_id=graph.clinic_b, appointment_id=appointment_b.pk
        ).pk
    # Clinic A publishes twice before clinic B publishes its first version.
    text_a_v1 = _publish(graph, graph.clinic_a, "teleconsultation", "Sintetico A v1")
    text_a = _publish(graph, graph.clinic_a, "teleconsultation", "Sintetico A v2")
    marketing_a = _publish(graph, graph.clinic_a, "marketing", "Sintetico A mkt")
    text_b = _publish(graph, graph.clinic_b, "teleconsultation", "Sintetico B v1")
    marketing_b = _publish(graph, graph.clinic_b, "marketing", "Sintetico B mkt")
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        notice_a = consent.publish_notice(
            clinic_id=graph.clinic_a, topic="ai_use", text="Sintetico aviso A"
        ).pk
        notice_b = consent.publish_notice(
            clinic_id=graph.clinic_b, topic="ai_use", text="Sintetico aviso B"
        ).pk
    session_a = _session(
        graph.clinic_a, setup_a.enrollment_id, reader, graph.organization_a
    )
    session_b = _session(
        graph.clinic_b, setup_b.enrollment_id, reader, graph.organization_a
    )
    other_session_b = _session(graph.clinic_b, other_b, reader, graph.organization_a)
    acceptance_a = _accept(session_a, text_a, "teleconsultation")
    with runtime_role(), patient_session_context(session_a):
        revocation_a = consent.revoke_consent(acceptance_id=acceptance_a).pk
    refusal_a = _refuse(session_a, marketing_a, "marketing")
    acceptance_b = _accept(session_b, text_b, "teleconsultation")
    refusal_b = _refuse(session_b, marketing_b, "marketing")
    other_refusal_b = _refuse(other_session_b, marketing_b, "marketing")
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        disclosure_a = consent.record_ai_disclosure(
            clinic_id=graph.clinic_a,
            encounter_id=encounter_a,
            informed=True,
            refused=False,
        ).pk
        acknowledgment_a = consent.acknowledge_participant(
            clinic_id=graph.clinic_a,
            session_id=encounter_a,
            participant_kind="caregiver",
        ).pk
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        disclosure_b = consent.record_ai_disclosure(
            clinic_id=graph.clinic_b,
            encounter_id=encounter_b,
            informed=True,
            refused=True,
        ).pk
        acknowledgment_b = consent.acknowledge_participant(
            clinic_id=graph.clinic_b,
            session_id=encounter_b,
            participant_kind="interpreter",
        ).pk
    return ScopeWorld(
        graph=graph,
        reader=reader,
        patient=setup_a.patient_id,
        other_patient=other.patient.pk,
        enrollment_a=setup_a.enrollment_id,
        enrollment_b=setup_b.enrollment_id,
        other_a=other.enrollment.pk,
        other_b=other_b,
        session_a=session_a,
        session_b=session_b,
        other_session_b=other_session_b,
        text_a_v1=text_a_v1,
        text_a=text_a,
        text_b=text_b,
        marketing_a=marketing_a,
        marketing_b=marketing_b,
        notice_a=notice_a,
        notice_b=notice_b,
        acceptance_a=acceptance_a,
        revocation_a=revocation_a,
        acceptance_b=acceptance_b,
        refusal_a=refusal_a,
        refusal_b=refusal_b,
        other_refusal_b=other_refusal_b,
        encounter_a=encounter_a,
        other_encounter_a=other_encounter_a,
        encounter_b=encounter_b,
        disclosure_a=disclosure_a,
        disclosure_b=disclosure_b,
        acknowledgment_a=acknowledgment_a,
        acknowledgment_b=acknowledgment_b,
    )


def _pks(rows: object) -> set[UUID]:
    assert isinstance(rows, list)
    return {row.pk for row in rows}


# ---------------------------------------------------------------------------
# Python cells.


def test_staff_reads_return_exactly_the_named_clinics_records(
    rbac_graph: RbacGraph,
) -> None:
    """B1: a reader permitted in A and B names B and gets only B's records."""
    w = seed_scope_world(rbac_graph)
    unknown = uuid4()
    with runtime_role(), tenant_context(w.reader, w.organization):
        receipts = {
            (clinic, enrollment): _pks(
                consent.staff_receipts(clinic_id=clinic, enrollment_id=enrollment)
            )
            for clinic in (w.clinic_a, w.clinic_b)
            for enrollment in (w.enrollment_a, w.enrollment_b, w.other_b, unknown)
        }
        refusals = {
            (clinic, enrollment): _pks(
                consent.staff_refusals(clinic_id=clinic, enrollment_id=enrollment)
            )
            for clinic in (w.clinic_a, w.clinic_b)
            for enrollment in (w.enrollment_a, w.enrollment_b, w.other_b, unknown)
        }
        disclosures = {
            (clinic, encounter): getattr(
                consent.ai_disclosure_status(clinic_id=clinic, encounter_id=encounter),
                "pk",
                None,
            )
            for clinic in (w.clinic_a, w.clinic_b)
            for encounter in (w.encounter_a, w.encounter_b, unknown)
        }
        future = {
            (clinic, enrollment): getattr(
                consent.consent_for_future_use(
                    clinic_id=clinic,
                    enrollment_id=enrollment,
                    purpose="teleconsultation",
                ),
                "pk",
                None,
            )
            for clinic in (w.clinic_a, w.clinic_b)
            for enrollment in (w.enrollment_a, w.enrollment_b, unknown)
        }
    a, b = w.clinic_a, w.clinic_b
    assert receipts == {
        (a, w.enrollment_a): {w.acceptance_a},
        (a, w.enrollment_b): set(),
        (a, w.other_b): set(),
        (a, unknown): set(),
        (b, w.enrollment_a): set(),
        (b, w.enrollment_b): {w.acceptance_b},
        (b, w.other_b): set(),
        (b, unknown): set(),
    }
    assert refusals == {
        (a, w.enrollment_a): {w.refusal_a},
        (a, w.enrollment_b): set(),
        (a, w.other_b): set(),
        (a, unknown): set(),
        (b, w.enrollment_a): set(),
        (b, w.enrollment_b): {w.refusal_b},
        (b, w.other_b): {w.other_refusal_b},
        (b, unknown): set(),
    }
    assert disclosures == {
        (a, w.encounter_a): w.disclosure_a,
        (a, w.encounter_b): None,
        (a, unknown): None,
        (b, w.encounter_a): None,
        (b, w.encounter_b): w.disclosure_b,
        (b, unknown): None,
    }
    # A's acceptance is revoked; B's current text is B's version 1 even though
    # clinic A holds a newer version 2 of the same purpose.
    assert future == {
        (a, w.enrollment_a): None,
        (a, w.enrollment_b): None,
        (a, unknown): None,
        (b, w.enrollment_a): None,
        (b, w.enrollment_b): w.acceptance_b,
        (b, unknown): None,
    }


def test_unknown_and_foreign_clinics_are_refused_identically(
    rbac_graph: RbacGraph,
) -> None:
    """B1: an unknown clinic and every foreign clinic give one refusal.

    The reader holds receptionist in A and B and physician in clinic C of
    another organization; clinic D of the same organization has no role for
    them. Each read names clinic B's real records, so only the clinic decides.
    """
    w = seed_scope_world(rbac_graph)
    with owner_context(w.organization):
        unheld = Clinic.objects.create(
            organization_id=w.organization,
            name="Sintetico Clinic D",
            crm_uf="MG",
            timezone="America/Sao_Paulo",
        ).pk
    reads: dict[str, Callable[[UUID], object]] = {
        "staff_receipts": lambda clinic: consent.staff_receipts(
            clinic_id=clinic, enrollment_id=w.enrollment_b
        ),
        "staff_refusals": lambda clinic: consent.staff_refusals(
            clinic_id=clinic, enrollment_id=w.enrollment_b
        ),
        "ai_disclosure_status": lambda clinic: consent.ai_disclosure_status(
            clinic_id=clinic, encounter_id=w.encounter_b
        ),
        "consent_for_future_use": lambda clinic: consent.consent_for_future_use(
            clinic_id=clinic, enrollment_id=w.enrollment_b, purpose="teleconsultation"
        ),
    }
    clinics = {
        "permitted": w.clinic_b,
        "unknown": uuid4(),
        "other-organization": w.graph.clinic_c,
        "unheld-same-organization": unheld,
    }
    observed = {}
    with runtime_role(), transaction.atomic():
        for name, read in reads.items():
            for label, clinic in clinics.items():
                cell = run(w.reader, w.organization, partial(read, clinic))
                observed[name, label] = (
                    type(cell.error),
                    None if cell.error is None else cell.error.args,
                    cell.writes,
                    # The named clinic is the only input that differs.
                    [
                        tuple("<named>" if item == clinic else item for item in names)
                        for names in cell.names
                    ],
                )
        transaction.set_rollback(True)
    refusal = (
        current_context._UnauthorizedActorError,
        ("current actor unauthorized",),
        0,
        [
            ("demographics.read", "<named>", None),
            ("configuration.organization", "<named>", None),
        ],
    )
    for name in reads:
        error, args, _, names = observed[name, "permitted"]
        assert (error, args, names) == (
            type(None),
            None,
            [("demographics.read", "<named>", None)],
        ), name
        assert {
            label: observed[name, label] for label in clinics if label != "permitted"
        } == {
            "unknown": refusal,
            "other-organization": refusal,
            "unheld-same-organization": refusal,
        }, name


def test_patient_sessions_see_only_their_clinic(rbac_graph: RbacGraph) -> None:
    w = seed_scope_world(rbac_graph)
    with runtime_role(), patient_session_context(w.session_b):
        assert {t.pk for t in consent.available_texts()} == {
            w.text_b,
            w.marketing_b,
        }
        assert {n.pk for n in consent.available_notices()} == {w.notice_b}
        assert _pks(consent.patient_receipts()) == {w.acceptance_b}
        assert _pks(consent.patient_refusals()) == {w.refusal_b}
        # The offer resolves against clinic B's current text, not the newer A
        # version, and the stale-text check compares within the clinic.
        _, offer = consent.prepare_acceptance(text_id=w.text_b)
        again = consent.record_consent(
            offer=offer, purpose="teleconsultation", accepted=True
        )
        assert again.pk == w.acceptance_b
    # Refusal path: Q refuses clinic B's teleconsultation version 1 while
    # clinic A holds version 2; the stale-text check compares within B.
    q_refusal = _refuse(w.other_session_b, w.text_b, "teleconsultation")
    with owner_context(w.organization):
        stored = RefusalRecord.objects.get(pk=q_refusal)
        assert (stored.clinic_id, stored.text_id) == (w.clinic_b, w.text_b)
    with runtime_role(), patient_session_context(w.session_a):
        assert {t.pk for t in consent.available_texts()} == {
            w.text_a,
            w.marketing_a,
        }
        assert {n.pk for n in consent.available_notices()} == {w.notice_a}
        with pytest.raises(PatientAccessDeniedError):
            consent.prepare_acceptance(text_id=w.text_b)


def test_writes_store_the_exact_scope(rbac_graph: RbacGraph) -> None:
    w = seed_scope_world(rbac_graph)
    org, a, b = w.organization, w.clinic_a, w.clinic_b
    with owner_context(org):
        texts = {
            row.pk: (row.organization_id, row.clinic_id, row.version)
            for row in ConsentText.objects.filter(purpose="teleconsultation")
        }
        assert texts[w.text_a] == (org, a, 2)
        assert texts[w.text_b] == (org, b, 1)
        notices = NoticeVersion.objects.in_bulk([w.notice_a, w.notice_b])
        assert (
            notices[w.notice_a].organization_id,
            notices[w.notice_a].clinic_id,
            notices[w.notice_a].version,
        ) == (org, a, 1)
        assert (
            notices[w.notice_b].organization_id,
            notices[w.notice_b].clinic_id,
            notices[w.notice_b].version,
        ) == (org, b, 1)
        stored: dict[
            UUID,
            tuple[
                type[ConsentAcceptance | RefusalRecord | AIUseDisclosure],
                tuple[UUID, UUID, UUID],
            ],
        ] = {
            w.acceptance_a: (ConsentAcceptance, (org, a, w.patient)),
            w.acceptance_b: (ConsentAcceptance, (org, b, w.patient)),
            w.refusal_a: (RefusalRecord, (org, a, w.patient)),
            w.refusal_b: (RefusalRecord, (org, b, w.patient)),
            w.other_refusal_b: (RefusalRecord, (org, b, w.other_patient)),
            w.disclosure_a: (AIUseDisclosure, (org, a, w.patient)),
            w.disclosure_b: (AIUseDisclosure, (org, b, w.patient)),
        }
        for pk, (model, scope) in stored.items():
            row = model._default_manager.get(pk=pk)
            assert (row.organization_id, row.clinic_id, row.patient_id) == scope
        revocation = ConsentRevocation.objects.get(pk=w.revocation_a)
        assert (revocation.organization_id, revocation.clinic_id) == (org, a)
        for pk, clinic in ((w.acknowledgment_a, a), (w.acknowledgment_b, b)):
            ack = ParticipantAcknowledgment.objects.get(pk=pk)
            assert (ack.organization_id, ack.clinic_id) == (org, clinic)


def test_attestations_bind_the_encounter_patients_enrollment(
    rbac_graph: RbacGraph,
) -> None:
    """The enrollment passed to has_permission is the encounter patient's own."""
    w = seed_scope_world(rbac_graph)
    graph = w.graph
    # Care-team-only physicians: K on P in A, L on Q in A, M on P in A and B.
    k = physician(graph, {w.clinic_a: (w.enrollment_a,)})
    l_ = physician(graph, {w.clinic_a: (w.other_a,)})
    m = physician(graph, {w.clinic_a: (w.enrollment_a,), w.clinic_b: (w.enrollment_b,)})
    cells: list[tuple[UUID, UUID, UUID, bool]] = [
        (k, w.clinic_a, w.encounter_a, True),
        (k, w.clinic_a, w.other_encounter_a, False),
        (l_, w.clinic_a, w.other_encounter_a, True),
        (l_, w.clinic_a, w.encounter_a, False),
        (m, w.clinic_a, w.encounter_a, True),
        (m, w.clinic_b, w.encounter_b, True),
        (m, w.clinic_b, w.encounter_a, False),
        (m, w.clinic_a, w.encounter_b, False),
    ]
    enrollment_of = {
        (w.clinic_a, w.encounter_a): w.enrollment_a,
        (w.clinic_a, w.other_encounter_a): w.other_a,
        (w.clinic_b, w.encounter_b): w.enrollment_b,
    }
    with runtime_role(), transaction.atomic():
        for actor, clinic, encounter, allowed in cells:
            for record in (
                lambda c=clinic, e=encounter: consent.record_ai_disclosure(
                    clinic_id=c,
                    encounter_id=e,
                    informed=True,
                    # Replay the seeded value: only authorization decides.
                    refused=e == w.encounter_b,
                ),
                lambda c=clinic, e=encounter: consent.acknowledge_participant(
                    clinic_id=c, session_id=e, participant_kind="companion"
                ),
            ):
                cell = run(actor, w.organization, record)
                # Spy: exactly the encounter patient's enrollment in the named
                # clinic, or none at all for an encounter of another clinic.
                assert cell.names == [
                    ("clinical.write", clinic, enrollment_of.get((clinic, encounter)))
                ], (actor, clinic, encounter)
                if allowed:
                    assert cell.error is None, (actor, clinic, encounter, cell.error)
                else:
                    assert isinstance(cell.error, CurrentActorError), cell.error
                    assert cell.error.args == ("current actor unauthorized",)
                    assert cell.writes == 0
        transaction.set_rollback(True)


# ---------------------------------------------------------------------------
# SQL cells: direct clinic_app INSERTs that cross exactly one binding.


def _outcome(insert: Callable[[], object]) -> str | None:
    cause: BaseException | None = None
    try:
        with transaction.atomic():
            insert()
            transaction.set_rollback(True)
    except DatabaseError as error:
        cause = error.__cause__
    if cause is None:
        return None
    assert isinstance(cause, psycopg.Error), cause
    return f"{cause.sqlstate}:{cause.diag.message_primary}"


def _publication(
    model: type[ConsentText | NoticeVersion], **scope: UUID
) -> Callable[[], object]:
    def insert() -> object:
        clinic = scope["clinic_id"]
        field = (
            {"purpose": "research_model_improvement"}
            if model is ConsentText
            else {"topic": "care_processing"}
        )
        return model.objects.create(
            organization_id=scope["organization_id"],
            clinic_id=clinic,
            version=1,
            text="Sintetico",
            language="pt-BR",
            digest="0" * 64,
            published_by_id=scope["actor"],
            **field,
        )

    return insert


def _attestation(
    actor: UUID, kind: str, scope: tuple[UUID, UUID, UUID, UUID]
) -> Callable[[], object]:
    """``scope`` is (organization, clinic, encounter, patient)."""
    organization, clinic, encounter, patient = scope

    def insert() -> object:
        if kind == "disclosure":
            return AIUseDisclosure.objects.create(
                organization_id=organization,
                clinic_id=clinic,
                encounter_id=encounter,
                patient_id=patient,
                informed=True,
                refused=False,
                recorded_by_id=actor,
                recorded_at=timezone.now(),
            )
        return ParticipantAcknowledgment.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            session_id=encounter,
            participant_kind="companion",
            acknowledged_by_clinician_id=actor,
            acknowledged_at=timezone.now(),
        )

    return insert


def _patient_row(
    model: type[ConsentAcceptance | RefusalRecord],
    session: UUID,
    **scope: UUID,
) -> Callable[[], object]:
    def insert() -> object:
        stamp = "accepted_at" if model is ConsentAcceptance else "refused_at"
        return model.objects.create(
            organization_id=scope["organization_id"],
            clinic_id=scope["clinic_id"],
            patient_id=scope["patient_id"],
            enrollment_id=scope["enrollment"],
            text_id=scope["text"],
            patient_session_id=session,
            **{stamp: timezone.now()},
        )

    return insert


def test_trigger_refuses_every_crossed_binding(rbac_graph: RbacGraph) -> None:
    """Direct clinic_app INSERTs, bypassing Python, cross one binding each."""
    w = seed_scope_world(rbac_graph)
    graph = w.graph
    org, foreign_org = w.organization, graph.organization_b
    a, b = w.clinic_a, w.clinic_b
    admin = graph.clinic_admin
    publications = {
        "text-control": _publication(
            ConsentText, organization_id=org, clinic_id=a, actor=admin
        ),
        "text-foreign-org": _publication(
            ConsentText, organization_id=foreign_org, clinic_id=a, actor=admin
        ),
        "notice-control": _publication(
            NoticeVersion, organization_id=org, clinic_id=b, actor=admin
        ),
        "notice-foreign-org": _publication(
            NoticeVersion, organization_id=foreign_org, clinic_id=b, actor=admin
        ),
    }
    with runtime_role(), tenant_context(admin, org):
        outcomes = {name: _outcome(insert) for name, insert in publications.items()}
    assert outcomes == {
        "text-control": None,
        "text-foreign-org": "23514:invalid consent publication",
        "notice-control": None,
        "notice-foreign-org": "23514:invalid notice publication",
    }
    # Q's clinic A encounter has no disclosure yet. The care-team physician for
    # Q is permitted; M holds P in A and B.
    q_care = physician(graph, {a: (w.other_a,)})
    m = physician(graph, {a: (w.enrollment_a,), b: (w.enrollment_b,)})
    target = w.other_encounter_a
    cells = {
        "participant-control": (
            q_care,
            _attestation(q_care, "participant", (org, a, target, w.other_patient)),
        ),
        "participant-foreign-org": (
            q_care,
            _attestation(
                q_care, "participant", (foreign_org, a, target, w.other_patient)
            ),
        ),
        "participant-other-clinic": (
            m,
            _attestation(m, "participant", (org, b, w.encounter_a, w.patient)),
        ),
        "disclosure-control": (
            q_care,
            _attestation(q_care, "disclosure", (org, a, target, w.other_patient)),
        ),
        "disclosure-foreign-org": (
            q_care,
            _attestation(
                q_care, "disclosure", (foreign_org, a, target, w.other_patient)
            ),
        ),
        # B2: a permitted physician binds Q's encounter to P's patient id.
        "disclosure-wrong-patient": (
            q_care,
            _attestation(q_care, "disclosure", (org, a, target, w.patient)),
        ),
        "disclosure-other-clinic": (
            m,
            _attestation(m, "disclosure", (org, b, w.encounter_a, w.patient)),
        ),
    }
    outcomes = {}
    for name, (actor, insert) in cells.items():
        with runtime_role(), tenant_context(actor, org):
            outcomes[name] = _outcome(insert)
    refused = "42501:consent staff authority required"
    assert outcomes == {
        "participant-control": None,
        "participant-foreign-org": "23514:invalid participant acknowledgment",
        "participant-other-clinic": refused,
        "disclosure-control": None,
        "disclosure-foreign-org": "23514:invalid AI-use disclosure",
        "disclosure-wrong-patient": "23514:invalid AI-use disclosure",
        "disclosure-other-clinic": refused,
    }
    # Patient rows from P's clinic B session, crossing one binding at a time.
    research_b = _publish(graph, b, "research_model_improvement", "Sintetico B")
    research_a = _publish(graph, a, "research_model_improvement", "Sintetico A")
    valid = {
        "organization_id": org,
        "clinic_id": b,
        "patient_id": w.patient,
        "enrollment": w.enrollment_b,
        "text": research_b,
    }
    crossings = {
        "control": {},
        "foreign-org": {"organization_id": foreign_org},
        "session-clinic": {"clinic_id": a},
        "text-clinic": {"text": research_a},
        "patient": {"patient_id": w.other_patient},
    }
    authority = "42501:patient consent authority required"
    for model, binding in (
        (ConsentAcceptance, "23514:invalid consent binding"),
        (RefusalRecord, "23514:invalid refusal binding"),
    ):
        with runtime_role(), patient_session_context(w.session_b):
            observed = {
                name: _outcome(_patient_row(model, w.session_b, **(valid | change)))
                for name, change in crossings.items()
            }
        assert observed == {
            "control": None,
            "foreign-org": authority,
            "session-clinic": authority,
            "text-clinic": binding,
            "patient": binding,
        }, model.__name__

    # Revoking P's clinic A acceptance from P's clinic B session.
    def revoke(acceptance: UUID) -> Callable[[], object]:
        return lambda: ConsentRevocation.objects.create(
            organization_id=org,
            clinic_id=b,
            acceptance_id=acceptance,
            patient_session_id=w.session_b,
            revoked_at=timezone.now(),
        )

    with runtime_role(), patient_session_context(w.session_b):
        crossed = _outcome(revoke(w.acceptance_a))
        own = _outcome(revoke(w.acceptance_b))
    assert (crossed, own) == ("23514:invalid revocation binding", None)


_CONSENT_MODELS = (
    ConsentText,
    NoticeVersion,
    ConsentAcceptance,
    RefusalRecord,
    ConsentRevocation,
    AIUseDisclosure,
    ParticipantAcknowledgment,
)


def _visible() -> dict[str, set[UUID]]:
    """Every row the current role and GUCs can read, per consent table."""
    return {
        model.__name__: set(model._default_manager.values_list("pk", flat=True))
        for model in _CONSENT_MODELS
    }


def test_rls_reads_bind_the_patient_session_and_tenant(rbac_graph: RbacGraph) -> None:
    """Direct reads, no Python filter: the policies alone decide the rows."""
    w = seed_scope_world(rbac_graph)
    sessions = {}
    for label, session in (
        ("P in A", w.session_a),
        ("P in B", w.session_b),
        ("Q in B", w.other_session_b),
    ):
        with runtime_role(), patient_session_context(session):
            sessions[label] = _visible()
    texts_a = {w.text_a_v1, w.text_a, w.marketing_a}
    texts_b = {w.text_b, w.marketing_b}
    assert sessions == {
        "P in A": {
            "ConsentText": texts_a,
            "NoticeVersion": {w.notice_a},
            "ConsentAcceptance": {w.acceptance_a},
            "RefusalRecord": {w.refusal_a},
            "ConsentRevocation": {w.revocation_a},
            "AIUseDisclosure": {w.disclosure_a},
            "ParticipantAcknowledgment": set(),
        },
        # P's clinic A disclosure stays in A (clinic binding).
        "P in B": {
            "ConsentText": texts_b,
            "NoticeVersion": {w.notice_b},
            "ConsentAcceptance": {w.acceptance_b},
            "RefusalRecord": {w.refusal_b},
            "ConsentRevocation": set(),
            "AIUseDisclosure": {w.disclosure_b},
            "ParticipantAcknowledgment": set(),
        },
        # P's clinic B disclosure is not Q's (patient binding).
        "Q in B": {
            "ConsentText": texts_b,
            "NoticeVersion": {w.notice_b},
            "ConsentAcceptance": set(),
            "RefusalRecord": {w.other_refusal_b},
            "ConsentRevocation": set(),
            "AIUseDisclosure": set(),
            "ParticipantAcknowledgment": set(),
        },
    }
    # The maintenance role sees its tenant's rows and nothing of another.
    tenants = {}
    for label, organization in (("A", w.organization), ("B", w.graph.organization_b)):
        with owner_context(organization):
            tenants[label] = _visible()
    assert tenants == {
        "A": {
            "ConsentText": texts_a | texts_b,
            "NoticeVersion": {w.notice_a, w.notice_b},
            "ConsentAcceptance": {w.acceptance_a, w.acceptance_b},
            "RefusalRecord": {w.refusal_a, w.refusal_b, w.other_refusal_b},
            "ConsentRevocation": {w.revocation_a},
            "AIUseDisclosure": {w.disclosure_a, w.disclosure_b},
            "ParticipantAcknowledgment": {w.acknowledgment_a, w.acknowledgment_b},
        },
        "B": {model.__name__: set() for model in _CONSENT_MODELS},
    }


def _bind_gucs(**settings: UUID) -> None:
    with connection.cursor() as cursor:
        for setting, value in settings.items():
            cursor.execute(
                "SELECT pg_catalog.set_config(%s, %s, true)",
                [f"app.{setting}", str(value)],
            )


def test_owner_inserts_bind_the_tenant(rbac_graph: RbacGraph) -> None:
    """Maintenance-role INSERTs of organization A rows under tenant A and B."""
    w = seed_scope_world(rbac_graph)
    graph = w.graph
    org, a, b = w.organization, w.clinic_a, w.clinic_b
    q_care = physician(graph, {a: (w.other_a,)})
    target = w.other_encounter_a
    staff = {
        "text": (
            graph.clinic_admin,
            _publication(
                ConsentText, organization_id=org, clinic_id=b, actor=graph.clinic_admin
            ),
        ),
        "notice": (
            graph.clinic_admin,
            _publication(
                NoticeVersion,
                organization_id=org,
                clinic_id=b,
                actor=graph.clinic_admin,
            ),
        ),
        "participant": (
            q_care,
            _attestation(q_care, "participant", (org, a, target, w.other_patient)),
        ),
        "disclosure": (
            q_care,
            _attestation(q_care, "disclosure", (org, a, target, w.other_patient)),
        ),
    }
    # Q in clinic B has neither accepted nor refused B's teleconsultation text;
    # P revokes the live clinic B acceptance.
    q_row = {
        "organization_id": org,
        "clinic_id": b,
        "patient_id": w.other_patient,
        "enrollment": w.other_b,
        "text": w.text_b,
    }
    patient = {
        "acceptance": (
            w.other_session_b,
            _patient_row(ConsentAcceptance, w.other_session_b, **q_row),
        ),
        "refusal": (
            w.other_session_b,
            _patient_row(RefusalRecord, w.other_session_b, **q_row),
        ),
        "revocation": (
            w.session_b,
            lambda: ConsentRevocation.objects.create(
                organization_id=org,
                clinic_id=b,
                acceptance_id=w.acceptance_b,
                patient_session_id=w.session_b,
                revoked_at=timezone.now(),
            ),
        ),
    }
    outcomes = {}
    for tenant, organization in (("A", org), ("B", graph.organization_b)):
        for name, (actor, insert) in staff.items():
            with owner_context(organization):
                _bind_gucs(current_user_id=actor)
                outcomes[tenant, name] = _outcome(insert)
        for name, (session, insert) in patient.items():
            with owner_context(organization):
                _bind_gucs(current_patient_session=session)
                outcomes[tenant, name] = _outcome(insert)
    staff_refused = "42501:consent staff authority required"

    def rls(table: str) -> str:
        return f'42501:new row violates row-level security policy for table "{table}"'

    assert outcomes == {
        **{("A", name): None for name in (*staff, *patient)},
        ("B", "text"): staff_refused,
        ("B", "notice"): staff_refused,
        ("B", "participant"): staff_refused,
        ("B", "disclosure"): staff_refused,
        ("B", "acceptance"): rls("consent_consentacceptance"),
        ("B", "refusal"): rls("consent_refusalrecord"),
        ("B", "revocation"): rls("consent_consentrevocation"),
    }


def test_consent_audit_chains_within_its_organization(rbac_graph: RbacGraph) -> None:
    """A patient consent event links to its own organization's previous hash."""
    w = seed_scope_world(rbac_graph)
    graph = w.graph
    research = _publish(graph, w.clinic_b, "research_model_improvement", "Sintetico")
    with runtime_role(), patient_session_context(w.session_b):
        _, offer = consent.prepare_acceptance(text_id=research)
    # Another organization appends right before the patient event, so its row
    # is the newest in the whole table when consent_audit picks the previous
    # hash; verification fails if the event linked to that row.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        record_phase1_event(
            "consent.receipts.viewed",
            clinic_id=graph.clinic_c,
            affected_record_id=uuid4(),
        )
    with runtime_role(), patient_session_context(w.session_b):
        refusal = consent.record_refusal(
            offer=offer, purpose="research_model_improvement"
        ).pk
    with runtime_role(), tenant_context(w.reader, graph.organization_a):
        chain = verify_chain(graph.organization_a)
    assert chain.valid
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event "
            "WHERE affected_record_id=%s AND event_type='consent.refused'",
            [str(refusal)],
        )
        assert cursor.fetchone() == (1,)
