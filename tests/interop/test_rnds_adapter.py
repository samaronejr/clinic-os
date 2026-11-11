"""RNDS laboratory-result adapter behind EG-7: profile, gate, consent, transports.

The definitions package was unavailable (HTTP 404, 2026-11-10), so profile
validation is pinned to the guide's documentation example: the example passes
the validator once its redactions are filled, and the builder reproduces it.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.consent.services import revoke_consent
from apps.core import integration
from apps.core.integration import execute_operation as execute_as_owner
from apps.core.secrets import SecretUnavailableError, credential_store
from apps.intake.data_requests import submit_data_subject_request
from apps.intake.patient_access import patient_session_context
from apps.interop.errors import (
    InteropAccessDeniedError,
    InteropConflictError,
    RndsIneligibleError,
    RndsUnavailableError,
    RndsValidationError,
    ValidationIssue,
)
from apps.interop.models import RndsSubmission
from apps.interop.rnds_bundle import (
    DEFINITIONS,
    DOCUMENTED_CODES,
    PROFILES,
    Establishment,
    LabExam,
    build_bundle,
    parse_exam,
    validate_bundle,
)
from apps.interop.rnds_gate import require_rnds_homologation
from apps.interop.rnds_transport import (
    FIXTURE_ESTABLISHMENT,
    FIXTURE_TRANSPORT,
    FixtureRndsTransport,
    MtlsRndsTransport,
    RndsAnswer,
    RndsCredential,
    RndsEndpoints,
    RndsTransportError,
    credential_names,
    load_credential,
    parse_answer,
    recorded_answers,
    transport_for,
)
from apps.interop.services import RndsReceipt, exchange_clinical_record
from apps.orders.services import acknowledge_result, ingest_result
from apps.providers.services import is_live
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from interop.rnds_support import (
    CPF,
    EXAM,
    accept_consent,
    acknowledged_result,
    another_result,
    issue,
    local_rnds,
    open_homologation,
    patient_session,
    pkcs12_bundle,
    publish_consent,
    record_cpf,
    rnds_world,
)
from orders.support import submission
from patient_service_support import runtime_role
from provider_gate_support import activate_capability

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_django.fixtures import SettingsWrapper

    from interop.rnds_support import RndsWorld
    from rbac_fixtures import RbacGraph

FIXTURES = Path(__file__).parents[2] / "apps" / "interop" / "rnds_fixtures"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
db = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def fixture_transport() -> Iterator[None]:
    FIXTURE_TRANSPORT.reset()
    yield
    FIXTURE_TRANSPORT.reset()


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    """Keep committed operations in the outbox; each test executes them itself."""
    sent: list[UUID] = []
    monkeypatch.setattr(integration, "_dispatch", sent.append)
    return sent


def execute_operation(operation_id: UUID) -> str:
    """Run the worker boundary exactly as the runtime role does."""
    with runtime_role():
        return execute_as_owner(operation_id)


def submit(
    world: RndsWorld, *, command: UUID | None = None, **changes: object
) -> RndsReceipt:
    with as_actor(world.graph, world.graph.physician):
        return exchange_clinical_record(
            clinic_id=world.graph.clinic_a,
            result_version_id=world.version_id,
            exam={**EXAM, **changes},
            command_id=command or uuid4(),
        )


STORED = (
    "state",
    "revision",
    "rnds_identifier",
    "answer_code",
    "decided_at",
    "bundle_sha256",
    "environment",
    "profile",
)


def stored(database_url: str, submission_id: UUID) -> dict[str, Any]:
    """Read one submission past RLS, as the export tests read their receipts."""
    with psycopg.connect(database_url) as raw:
        row = raw.execute(
            f"SELECT {','.join(STORED)} FROM clinic_app.interop_rndssubmission "  # noqa: S608
            "WHERE id=%s",
            [submission_id],
        ).fetchone()
    assert row is not None
    return dict(zip(STORED, row, strict=True))


def submissions(database_url: str, version_id: UUID | None = None) -> int:
    """Count stored submissions, optionally only those of one result version."""
    with psycopg.connect(database_url) as raw:
        row = raw.execute(
            "SELECT count(*) FROM clinic_app.interop_rndssubmission "
            "WHERE %s::uuid IS NULL OR result_version_id=%s::uuid",
            [version_id, version_id],
        ).fetchone()
    assert row is not None
    return int(row[0])


def operation_for(graph: RbacGraph, submission_id: UUID) -> IntegrationOperation:
    with owner_context(graph.organization_a):
        return IntegrationOperation.objects.get(subject_id=submission_id)


def events(graph: RbacGraph, record_id: UUID) -> list[str]:
    with owner_context(graph.organization_a):
        return list(
            AuditEvent.objects.filter(affected_record_id=str(record_id))
            .order_by("seq")
            .values_list("event_type", flat=True)
        )


def _example() -> dict[str, Any]:
    return cast(
        "dict[str, Any]", json.loads((FIXTURES / "rel-example.json").read_text())
    )


def _filled_example() -> dict[str, Any]:
    """The documentation bundle with its redacted identifiers filled synthetically."""
    bundle = copy.deepcopy(_example()["bundle"])
    composition, observation, _ = (entry["resource"] for entry in bundle["entry"])
    for resource in (composition, observation):
        resource["subject"]["identifier"]["value"] = CPF
    composition["author"][0]["identifier"]["value"] = FIXTURE_ESTABLISHMENT.cnes
    observation["performer"][0]["identifier"]["value"] = FIXTURE_ESTABLISHMENT.performer
    return cast("dict[str, Any]", bundle)


def _instants(bundle: dict[str, Any], value: str) -> dict[str, Any]:
    bundle["timestamp"] = bundle["meta"]["lastUpdated"] = value
    bundle["entry"][0]["resource"]["date"] = value
    bundle["entry"][1]["resource"]["issued"] = value
    return bundle


def test_definitions_record_the_unavailable_package_and_documented_codes() -> None:
    source = _example()["source"]
    assert DEFINITIONS["package"].startswith("unavailable: ")
    assert "404" in DEFINITIONS["package"]
    assert (source["url"], source["fetched"], source["example_date"]) == (
        DEFINITIONS["guide"],
        DEFINITIONS["fetched"],
        DEFINITIONS["example_date"],
    )
    text = json.dumps(_example()["bundle"])
    for codes in DOCUMENTED_CODES.values():
        assert all(f'"{code}"' in text or f"({code})" in text for code in codes)
    profiles = {
        entry["resource"]["meta"]["profile"][0]
        for entry in _example()["bundle"]["entry"]
    }
    assert profiles == set(PROFILES.values())


def test_builder_reproduces_the_documentation_example() -> None:
    example = _filled_example()
    assert validate_bundle(example) == ()
    observation = example["entry"][1]["resource"]
    exam = LabExam(
        exam_code=observation["code"]["coding"][0]["code"],
        sus_subgroup=observation["category"][0]["coding"][0]["code"],
        result_code=observation["valueCodeableConcept"]["coding"][0]["code"],
        method=observation["method"]["text"],
        reference_range=observation["referenceRange"][0]["text"],
        specimen_type=example["entry"][2]["resource"]["type"]["coding"][0]["code"],
        issued_at=datetime.fromisoformat(observation["issued"]),
    )
    requester = example["identifier"]["system"].rsplit("BRRNDS-", 1)[1]
    built = build_bundle(
        submission_id=UUID(example["id"]),
        composed_at=datetime.fromisoformat(example["timestamp"]),
        exam=exam,
        patient=CPF,
        establishment=Establishment(
            requester_id=requester,
            cnes=FIXTURE_ESTABLISHMENT.cnes,
            performer=FIXTURE_ESTABLISHMENT.performer,
            requester_cns=FIXTURE_ESTABLISHMENT.requester_cns,
        ),
    )
    assert validate_bundle(built) == ()
    same = "2020-10-02T11:57:31-03:00"
    assert _instants(cast("dict[str, Any]", built), same) == _instants(example, same)


def test_validator_surfaces_every_violation_by_path_never_by_value() -> None:
    bundle = _filled_example()
    composition, observation, specimen = (
        entry["resource"] for entry in bundle["entry"]
    )
    observation["status"] = "preliminary"
    observation["code"]["coding"][0]["code"] = "94507-2"
    observation["category"][0]["coding"][0]["code"] = "0215"
    observation["subject"]["identifier"]["value"] = "11111111111"
    composition["meta"]["profile"] = ["http://example.invalid/profile"]
    del composition["title"]
    specimen["note"] = "texto livre"
    issues = set(validate_bundle(bundle))
    assert {
        ValidationIssue("Observation.status", "status_not_final"),
        ValidationIssue("Observation.code.coding.code", "invalid_check_digit"),
        ValidationIssue("Observation.category.coding.code", "code_not_documented"),
        ValidationIssue("Observation.subject.identifier.value", "invalid_identifier"),
        ValidationIssue("Observation.subject.identifier.value", "subject_mismatch"),
        ValidationIssue("Composition.meta.profile", "invalid_profile"),
        ValidationIssue("Composition.title", "required"),
        ValidationIssue("Specimen.note", "unexpected_element"),
    } <= issues
    rendered = repr(issues)
    assert "11111111111" not in rendered
    assert CPF not in rendered
    assert "texto livre" not in rendered


def test_exam_document_errors_are_reported_together() -> None:
    document = {
        **EXAM,
        "exam_code": "94507-2",
        "sus_subgroup": "0215",
        "result_code": "x",
        "reference_range": "faixa\x00",
        "issued_at": "2030-01-01T00:00:00+00:00",
        "patient_name": "Paciente Sintético",
    }
    del document["method"]
    with pytest.raises(RndsValidationError) as caught:
        parse_exam(document, now=NOW)
    assert caught.value.issues == (
        ValidationIssue("exam.patient_name", "unknown_field"),
        ValidationIssue("exam.method", "required"),
        ValidationIssue("exam.exam_code", "invalid_check_digit"),
        ValidationIssue("exam.sus_subgroup", "code_not_documented"),
        ValidationIssue("exam.result_code", "invalid_format"),
        ValidationIssue("exam.reference_range", "invalid_text"),
        ValidationIssue("exam.issued_at", "invalid_datetime"),
    )
    invalid_documents: tuple[object, ...] = (
        [],
        {**EXAM, "issued_at": "2026-11-09T10:30:12"},
    )
    for invalid in invalid_documents:
        with pytest.raises(RndsValidationError):
            parse_exam(invalid, now=NOW)
    exam = parse_exam(EXAM, now=NOW)
    assert exam.issued_at == datetime.fromisoformat(str(EXAM["issued_at"]))


@db
def test_fixture_submission_is_sealed_sent_and_accepted(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    receipt = submit(world)
    assert (receipt.state, receipt.created) == ("pending", True)
    operation = operation_for(world.graph, receipt.submission_id)
    row = stored(superuser_database_url, receipt.submission_id)
    assert dispatched == [operation.pk]
    assert (operation.kind, operation.provider, operation.subject_type) == (
        "action",
        "interop-rnds-v1",
        "interop.rnds_submission",
    )
    assert operation.payload_digest == row["bundle_sha256"]
    assert operation.actor_id == world.graph.physician
    assert (row["environment"], row["profile"], row["state"], row["revision"]) == (
        "homologation",
        "BRResultadoExameLaboratorial-1.1",
        "pending",
        1,
    )
    with psycopg.connect(superuser_database_url) as raw:
        sealed = raw.execute(
            "SELECT position(%s::bytea in bundle) "
            "FROM clinic_app.interop_rndssubmission",
            [CPF.encode()],
        ).fetchone()
    assert sealed == (0,)
    assert execute_operation(operation.pk) == "succeeded"
    (request,) = FIXTURE_TRANSPORT.requests
    with as_actor(world.graph, world.graph.physician):
        bundle_bytes = RndsSubmission.objects.get(pk=receipt.submission_id).bundle
    assert request.body == bundle_bytes
    assert hashlib.sha256(request.body).hexdigest() == row["bundle_sha256"]
    assert request.headers["Authorization"] == FIXTURE_ESTABLISHMENT.requester_cns
    bundle = json.loads(request.body)
    assert validate_bundle(bundle) == ()
    assert bundle["id"] == bundle["identifier"]["value"] == str(receipt.submission_id)
    assert bundle["entry"][0]["resource"]["subject"]["identifier"]["value"] == CPF
    accepted = stored(superuser_database_url, receipt.submission_id)
    assert (accepted["state"], accepted["revision"], accepted["answer_code"]) == (
        "accepted",
        2,
        "",
    )
    assert (
        accepted["rnds_identifier"]
        == FIXTURE_TRANSPORT.accepted[str(receipt.submission_id)]
    )
    assert accepted["decided_at"] is not None
    done = operation_for(world.graph, receipt.submission_id)
    assert done.status == "succeeded"
    assert done.provider_reference == f"rnds:{accepted['rnds_identifier']}"
    assert events(world.graph, receipt.submission_id) == [
        "interop.rnds_submission.requested",
        "interop.rnds_submission.accepted",
    ]
    with owner_context(world.graph.organization_a):
        payloads = json.dumps(
            list(AuditEvent.objects.values_list("payload", flat=True)), default=str
        )
        outbox = json.dumps(list(IntegrationOperation.objects.values()), default=str)
    for text in (payloads, outbox):
        assert CPF not in text
        assert "Imunocromatogr" not in text


@db
def test_replays_converge_and_reuse_or_resubmission_is_refused(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    command = uuid4()
    first = submit(world, command=command)
    again = submit(world, command=command)
    assert (again.submission_id, again.state, again.created) == (
        first.submission_id,
        "pending",
        False,
    )
    with pytest.raises(InteropConflictError):
        submit(world, command=command, result_code="2")
    with pytest.raises(RndsIneligibleError) as caught:
        submit(world)
    assert caught.value.reason == "already_submitted"
    assert len(dispatched) == 1
    assert submissions(superuser_database_url) == 1


@db
def test_rnds_rejection_keeps_its_code_and_frees_the_version(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    receipt = submit(world)
    FIXTURE_TRANSPORT.script.append("EHR-ERR924")
    assert execute_operation(dispatched[0]) == "failed"
    rejected = stored(superuser_database_url, receipt.submission_id)
    assert (
        rejected["state"],
        rejected["answer_code"],
        rejected["rnds_identifier"],
    ) == (
        "rejected",
        "EHR-ERR924",
        "",
    )
    operation = operation_for(world.graph, receipt.submission_id)
    assert (operation.status, operation.last_error) == ("failed", "provider_rejected")
    assert events(world.graph, receipt.submission_id)[-1] == (
        "interop.rnds_submission.rejected"
    )
    retry = submit(world)
    assert retry.created
    assert retry.submission_id != receipt.submission_id


@db
def test_transport_failures_retry_and_a_landed_duplicate_converges(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    receipt = submit(world)
    (operation_id,) = dispatched
    FIXTURE_TRANSPORT.script.extend(["outage", "network", "token_expired"])
    assert execute_operation(operation_id) == "retry"
    assert execute_operation(operation_id) == "retry"
    assert stored(superuser_database_url, receipt.submission_id)["state"] == "pending"
    # The third attempt meets an expired token, refreshes it once and lands.
    assert execute_operation(operation_id) == "succeeded"
    assert len(FIXTURE_TRANSPORT.requests) == 4
    assert len({request.body for request in FIXTURE_TRANSPORT.requests}) == 1
    assert stored(superuser_database_url, receipt.submission_id)["state"] == "accepted"
    # Another result whose first delivery landed before its answer was lost.
    lost = submit(replace(world, version_id=another_result(world.graph, world.order)))
    FIXTURE_TRANSPORT.accepted[str(lost.submission_id)] = "already-recorded"
    assert execute_operation(dispatched[-1]) == "succeeded"
    duplicate = stored(superuser_database_url, lost.submission_id)
    assert (
        duplicate["state"],
        duplicate["answer_code"],
        duplicate["rnds_identifier"],
    ) == ("accepted", "EHR-ERR866", "")


@db
def test_an_answer_without_an_identifier_resends_the_same_bundle(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = rnds_world(rbac_graph, settings)
    receipt = submit(world)
    (operation_id,) = dispatched
    landed = FixtureRndsTransport.submit_bundle

    def unreadable(
        transport: FixtureRndsTransport, *, body: bytes, requester_cns: str
    ) -> RndsAnswer:
        # RNDS keeps the document, but its answer carries no readable location.
        landed(transport, body=body, requester_cns=requester_cns)
        return RndsAnswer(status=201)

    with monkeypatch.context() as patch:
        patch.setattr(FixtureRndsTransport, "submit_bundle", unreadable)
        assert execute_operation(operation_id) == "retry"
    assert stored(superuser_database_url, receipt.submission_id)["state"] == "pending"
    # The resent bundle meets its own landed copy and converges on accepted.
    assert execute_operation(operation_id) == "succeeded"
    assert len(FIXTURE_TRANSPORT.requests) == 2
    assert len({request.body for request in FIXTURE_TRANSPORT.requests}) == 1
    converged = stored(superuser_database_url, receipt.submission_id)
    assert (
        converged["state"],
        converged["answer_code"],
        converged["rnds_identifier"],
    ) == ("accepted", "EHR-ERR866", "")


@db
def test_a_lost_acceptance_then_exhausted_retries_never_make_a_second_document(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gate review B1: RNDS stores the first send but its answer is unreadable,
    # every retry then fails, and only a later command reaches RNDS again.
    world = rnds_world(rbac_graph, settings)
    first = submit(world)
    (operation_id,) = dispatched
    landed = FixtureRndsTransport.submit_bundle

    def unreadable(
        transport: FixtureRndsTransport, *, body: bytes, requester_cns: str
    ) -> RndsAnswer:
        landed(transport, body=body, requester_cns=requester_cns)
        return RndsAnswer(status=201)

    with monkeypatch.context() as patch:
        patch.setattr(FixtureRndsTransport, "submit_bundle", unreadable)
        outcomes = [execute_operation(operation_id)]
    FIXTURE_TRANSPORT.script.extend(["timeout"] * 4)
    outcomes += [execute_operation(operation_id) for _ in range(5)]
    assert outcomes == ["retry"] * 5 + ["failed"]
    assert len(FIXTURE_TRANSPORT.accepted) == 1
    again = submit(world)
    assert execute_operation(dispatched[-1]) == "succeeded"
    # RNDS still holds exactly one document for the result version.
    assert len(FIXTURE_TRANSPORT.accepted) == 1
    assert len({request.body for request in FIXTURE_TRANSPORT.requests}) == 1
    assert (again.submission_id, again.created) == (first.submission_id, True)
    assert submissions(superuser_database_url, world.version_id) == 1
    settled = stored(superuser_database_url, first.submission_id)
    assert (
        settled["state"],
        settled["answer_code"],
        settled["rnds_identifier"],
    ) == ("accepted", "EHR-ERR866", "")


@db
def test_a_security_code_under_422_never_releases_a_landed_document(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gate review B1-R2: RNDS stores the bundle, then answers a contradictory 422
    # carrying the security code EHR-ERR882, read by the real parser.
    world = rnds_world(rbac_graph, settings)
    first = submit(world)
    landed = FixtureRndsTransport.submit_bundle
    recorded = cast("dict[str, object]", recorded_answers()["outcomes"]["EHR-ERR882"])
    security = json.dumps(recorded["body"])

    def contradictory(
        transport: FixtureRndsTransport, *, body: bytes, requester_cns: str
    ) -> RndsAnswer:
        landed(transport, body=body, requester_cns=requester_cns)
        return parse_answer(422, "", security.encode())

    with monkeypatch.context() as patch:
        patch.setattr(FixtureRndsTransport, "submit_bundle", contradictory)
        assert execute_operation(dispatched[0]) == "failed"
    again = submit(world)
    assert execute_operation(dispatched[-1]) == "succeeded"
    # RNDS still holds exactly one document for the result version.
    assert len(FIXTURE_TRANSPORT.accepted) == 1
    assert again.submission_id == first.submission_id
    settled = stored(superuser_database_url, first.submission_id)
    assert (settled["state"], settled["answer_code"]) == ("accepted", "EHR-ERR866")


@db
def test_replaying_an_earlier_command_returns_its_receipt_without_arming(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    # Gate review B3: commands A and B both end uncertain; replaying A must not
    # arm a send that its own, finished outbox operation can never run.
    world = rnds_world(rbac_graph, settings)
    command_a, command_b = uuid4(), uuid4()
    first = submit(world, command=command_a)
    FIXTURE_TRANSPORT.script.extend(["EHR-ERR881", "EHR-ERR881"])
    assert execute_operation(dispatched[0]) == "failed"
    second = submit(world, command=command_b)
    assert (second.submission_id, second.created) == (first.submission_id, True)
    assert execute_operation(dispatched[-1]) == "failed"
    replayed = submit(world, command=command_a)
    assert (replayed.submission_id, replayed.state, replayed.created) == (
        first.submission_id,
        "uncertain",
        False,
    )
    assert len(dispatched) == 2
    assert stored(superuser_database_url, first.submission_id)["state"] == ("uncertain")
    # The latest command replays the same way, and a fresh command resends.
    assert submit(world, command=command_b).created is False
    fresh = submit(world)
    assert (fresh.submission_id, fresh.created) == (first.submission_id, True)
    assert execute_operation(dispatched[-1]) == "succeeded"
    assert stored(superuser_database_url, first.submission_id)["state"] == ("accepted")
    assert len(FIXTURE_TRANSPORT.accepted) == 1


@db
def test_exhausted_attempts_keep_the_document_until_its_resend_settles(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    receipt = submit(world)
    (operation_id,) = dispatched
    FIXTURE_TRANSPORT.script.extend(["timeout"] * 5)
    assert [execute_operation(operation_id) for _ in range(5)] == ["retry"] * 5
    # The sixth claim finds every attempt spent and fails the operation unsent.
    assert execute_operation(operation_id) == "failed"
    assert len(FIXTURE_TRANSPORT.requests) == 5
    operation = operation_for(world.graph, receipt.submission_id)
    assert operation.last_error == "attempts_exhausted"
    uncertain = stored(superuser_database_url, receipt.submission_id)
    assert (uncertain["state"], uncertain["answer_code"]) == (
        "uncertain",
        "undelivered",
    )
    # Only a resend settles a document RNDS may hold, even for the superuser.
    with psycopg.connect(superuser_database_url) as raw, pytest.raises(psycopg.Error):
        raw.execute(
            "UPDATE clinic_app.interop_rndssubmission SET state='rejected',"
            " revision=revision+1, answer_code='EHR-ERR924' WHERE id=%s",
            [receipt.submission_id],
        )
    with pytest.raises(RndsIneligibleError) as caught:
        submit(world, result_code="2")
    assert caught.value.reason == "delivery_unresolved"
    resent = submit(world)
    assert (resent.submission_id, resent.state, resent.created) == (
        receipt.submission_id,
        "pending",
        True,
    )
    assert execute_operation(dispatched[-1]) == "succeeded"
    settled = stored(superuser_database_url, receipt.submission_id)
    assert settled["state"] == "accepted"
    assert (
        settled["rnds_identifier"]
        == FIXTURE_TRANSPORT.accepted[str(receipt.submission_id)]
    )
    assert len({request.body for request in FIXTURE_TRANSPORT.requests}) == 1
    assert submissions(superuser_database_url, world.version_id) == 1


@db
def test_a_refusal_without_a_validation_verdict_keeps_the_document(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    receipt = submit(world)
    FIXTURE_TRANSPORT.script.append("EHR-ERR881")
    assert execute_operation(dispatched[0]) == "failed"
    refused = stored(superuser_database_url, receipt.submission_id)
    assert (refused["state"], refused["answer_code"]) == ("uncertain", "EHR-ERR881")
    resent = submit(world)
    assert resent.submission_id == receipt.submission_id
    assert execute_operation(dispatched[-1]) == "succeeded"
    assert stored(superuser_database_url, receipt.submission_id)["state"] == (
        "accepted"
    )
    assert events(world.graph, receipt.submission_id) == [
        "interop.rnds_submission.requested",
        "interop.rnds_submission.requested",
        "interop.rnds_submission.accepted",
    ]


@db
def test_non_consented_and_revoked_records_are_refused(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings, consented=False)
    with pytest.raises(RndsIneligibleError) as caught:
        submit(world)
    assert caught.value.reason == "consent_required"
    acceptance = accept_consent(world.session, world.text_id)
    with runtime_role(), patient_session_context(world.session):
        revoke_consent(acceptance_id=acceptance.pk)
    with pytest.raises(RndsIneligibleError) as revoked:
        submit(world)
    assert revoked.value.reason == "consent_required"
    assert dispatched == []
    assert submissions(superuser_database_url) == 0
    with owner_context(world.graph.organization_a):
        assert not IntegrationOperation.objects.filter(
            subject_type="interop.rnds_submission"
        ).exists()


@db
def test_a_dsar_objection_withdraws_the_consent_and_the_record_is_refused(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    with runtime_role(), patient_session_context(world.session):
        objection = submit_data_subject_request(
            clinic_id=world.graph.clinic_a,
            kind="objection",
            details="",
            purpose="rnds_exchange",
            idempotency_key=uuid4(),
        )
    # The patient's right to object withdraws the RNDS acceptance at once.
    assert (objection.kind, objection.state) == ("objection", "fulfilled")
    with pytest.raises(RndsIneligibleError) as caught:
        submit(world)
    assert caught.value.reason == "consent_required"
    assert dispatched == []
    assert submissions(superuser_database_url) == 0


@db
def test_consent_revoked_before_the_send_cancels_it_without_a_request(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    open_homologation(rbac_graph, settings)
    version_id, order = acknowledged_result(rbac_graph)
    record_cpf(rbac_graph, order.enrollment)
    session = patient_session(rbac_graph, order.enrollment)
    acceptance = accept_consent(session, publish_consent(rbac_graph))
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = exchange_clinical_record(
            clinic_id=rbac_graph.clinic_a,
            result_version_id=version_id,
            exam=EXAM,
            command_id=uuid4(),
        )
    with runtime_role(), patient_session_context(session):
        revoke_consent(acceptance_id=acceptance.pk)
    assert execute_operation(dispatched[0]) == "cancelled"
    assert FIXTURE_TRANSPORT.requests == []
    withdrawn = stored(superuser_database_url, receipt.submission_id)
    assert (withdrawn["state"], withdrawn["answer_code"]) == (
        "uncertain",
        "withdrawn",
    )


@db
def test_ineligible_records_are_refused_with_closed_reasons(
    rbac_graph: RbacGraph, settings: SettingsWrapper, dispatched: list[UUID]
) -> None:
    world = rnds_world(rbac_graph, settings)
    graph = world.graph
    unreviewed = another_result(graph, world.order, acknowledged=False)
    with pytest.raises(RndsIneligibleError) as caught:
        submit(replace(world, version_id=unreviewed))
    assert caught.value.reason == "not_acknowledged"
    source = submission(world.order)
    with as_actor(graph, graph.physician):
        original = ingest_result(clinic_id=graph.clinic_a, submission=source)
        acknowledge_result(
            clinic_id=graph.clinic_a,
            version_id=original.version_id,
            disposition="no_action",
        )
        ingest_result(
            clinic_id=graph.clinic_a,
            submission=replace(
                source,
                event_id=uuid4(),
                data=source.data + b"\ncorrected",
                result_id=original.result_id,
                predecessor_id=original.version_id,
            ),
        )
    with pytest.raises(RndsIneligibleError) as superseded:
        submit(replace(world, version_id=original.version_id))
    assert superseded.value.reason == "source_superseded"
    assert dispatched == []


@db
def test_patient_without_cns_or_cpf_is_ineligible(
    rbac_graph: RbacGraph, settings: SettingsWrapper, dispatched: list[UUID]
) -> None:
    open_homologation(rbac_graph, settings)
    version_id, order = acknowledged_result(rbac_graph)
    session = patient_session(rbac_graph, order.enrollment)
    accept_consent(session, publish_consent(rbac_graph))
    with (
        as_actor(rbac_graph, rbac_graph.physician),
        pytest.raises(RndsIneligibleError) as caught,
    ):
        exchange_clinical_record(
            clinic_id=rbac_graph.clinic_a,
            result_version_id=version_id,
            exam=EXAM,
            command_id=uuid4(),
        )
    assert caught.value.reason == "identifier_required"
    assert dispatched == []


@db
def test_unknown_foreign_and_unauthorized_records_are_denied_alike(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    graph = world.graph
    # Clinic B also holds an open fixture leg, so its refusal is the record's.
    open_homologation(graph, settings, clinic=graph.clinic_b)
    attempts = (
        (graph.physician, graph.clinic_a, uuid4()),
        (graph.physician, graph.clinic_b, world.version_id),
        (graph.shared_user, graph.clinic_a, world.version_id),
    )
    for actor, clinic, version in attempts:
        with as_actor(graph, actor), pytest.raises(InteropAccessDeniedError):
            exchange_clinical_record(
                clinic_id=clinic,
                result_version_id=version,
                exam=EXAM,
                command_id=uuid4(),
            )
    # The resolver refuses the same forged call on its own, as the runtime role.
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT * FROM clinic_app.interop_rnds_request(%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                graph.clinic_a,
                uuid4(),
                world.version_id,
                uuid4(),
                "0" * 64,
                uuid4(),
                b"x",
                "0" * 64,
            ],
        )
    assert dispatched == []
    assert submissions(superuser_database_url, world.version_id) == 0


@db
def test_the_gate_refuses_by_default_and_activation_never_opens_it(
    rbac_graph: RbacGraph, settings: SettingsWrapper, dispatched: list[UUID]
) -> None:
    world = rnds_world(rbac_graph, settings)
    settings.INTEROP_RNDS_HOMOLOGATION = False
    with pytest.raises(RndsUnavailableError):
        submit(world)
    settings.INTEROP_RNDS_HOMOLOGATION = True
    settings.CLINIC_DATA_MODE = "live"
    with pytest.raises(RndsUnavailableError):
        submit(world)
    settings.CLINIC_DATA_MODE = "synthetic"
    # The platform row reaches ``activated`` through the owner lifecycle, yet it
    # is not live under synthetic data and its provider is not the fixture.
    activate_capability("rnds")
    assert is_live("rnds", clinic_id=None) is False
    with pytest.raises(RndsUnavailableError):
        require_rnds_homologation(clinic_id=world.graph.clinic_b)
    with pytest.raises(RndsUnavailableError):
        transport_for("datasus-rnds")
    # Clinic A's own approved fixture sandbox still governs clinic A.
    assert require_rnds_homologation(clinic_id=world.graph.clinic_a)
    assert submit(world).created
    assert len(dispatched) == 1


@db
def test_database_refuses_raw_writes_and_illegal_transitions(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    dispatched: list[UUID],
    superuser_database_url: str,
) -> None:
    world = rnds_world(rbac_graph, settings)
    receipt = submit(world)
    graph = world.graph
    for statement in (
        "UPDATE clinic_app.interop_rndssubmission SET state='accepted'",
        "DELETE FROM clinic_app.interop_rndssubmission",
        "INSERT INTO clinic_app.interop_rndssubmission (id) VALUES (gen_random_uuid())",
    ):
        with (
            runtime_role(),
            tenant_context(graph.physician, graph.organization_a),
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement)
    refused = (
        "DELETE FROM clinic_app.interop_rndssubmission",
        "TRUNCATE clinic_app.interop_rndssubmission",
        "UPDATE clinic_app.interop_rndssubmission SET command_id=gen_random_uuid(),"
        " revision=revision+1",
        "UPDATE clinic_app.interop_rndssubmission SET state='accepted',"
        " revision=revision+1",
        "UPDATE clinic_app.interop_rndssubmission SET state='rejected',"
        " answer_code='withdrawn'",
        # Only a recorded validation verdict may reject; a security code may not.
        "UPDATE clinic_app.interop_rndssubmission SET state='rejected',"
        " revision=revision+1, answer_code='EHR-ERR882'",
    )
    for statement in refused:
        with (
            psycopg.connect(superuser_database_url) as raw,
            pytest.raises(psycopg.Error),
        ):
            raw.execute(statement)
    with (
        runtime_role(),
        pytest.raises(DatabaseError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT clinic_app.interop_rnds_settle(%s,'accepted','forged','')",
            [dispatched[0]],
        )
    assert stored(superuser_database_url, receipt.submission_id)["state"] == "pending"


@db
def test_submissions_are_visible_only_with_result_authority(
    rbac_graph: RbacGraph, settings: SettingsWrapper, dispatched: list[UUID]
) -> None:
    world = rnds_world(rbac_graph, settings)
    receipt = submit(world)
    graph = world.graph
    with as_actor(graph, graph.physician):
        assert RndsSubmission.objects.filter(pk=receipt.submission_id).exists()
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        assert not RndsSubmission.objects.exists()
    with runtime_role(), transaction.atomic():
        assert not RndsSubmission.objects.exists()
    assert dispatched


def _store_credential(
    root: Path, clinic: UUID, pkcs12: bytes, passphrase: bytes
) -> None:
    for name, content in zip(
        credential_names(clinic), (pkcs12, passphrase), strict=True
    ):
        path = root / f"{name}.credential"
        path.touch(mode=0o600)
        path.write_bytes(content)


def test_mtls_transport_obtains_a_token_and_posts_the_bundle(
    tmp_path: Path, synthetic_secret_backend: Path
) -> None:
    with local_rnds(tmp_path) as server:
        client = issue("Synthetic Establishment", issuer=server.ca)
        clinic = uuid4()
        passphrase = b"senha-sintetica-do-certificado"
        _store_credential(
            synthetic_secret_backend,
            clinic,
            pkcs12_bundle(client, server.ca, passphrase),
            passphrase + b"\n",
        )
        credential = load_credential(clinic)
        assert "senha" not in repr(credential)
        transport = MtlsRndsTransport(
            endpoints=RndsEndpoints(
                "localhost", "localhost", server.auth_port, server.ehr_port
            ),
            credential=credential,
            cafile=str(server.cafile),
        )
        body = json.dumps({"resourceType": "Bundle"}).encode()
        answer = transport.submit_bundle(body=body, requester_cns="700000000000005")
        assert (answer.status, answer.identifier, answer.codes) == (
            201,
            "local-0001",
            (),
        )
        server.recorded.answers.append(
            (
                401,
                {"Content-Type": "application/json"},
                json.dumps(
                    {
                        "resourceType": "OperationOutcome",
                        "issue": [{"diagnostics": "(EHR-ERR882) token expirado"}],
                    }
                ).encode(),
            )
        )
        refreshed = transport.submit_bundle(body=body, requester_cns="700000000000005")
        assert refreshed.status == 201
        assert server.recorded.tokens == 2
        headers, sent = server.recorded.bundles[0]
        assert sent == body
        assert headers["x-authorization-server"] == "Bearer local-token-1"
        assert headers["authorization"] == "700000000000005"
        assert server.recorded.bundles[-1][0]["x-authorization-server"] == (
            "Bearer local-token-2"
        )


def test_mtls_transport_refuses_expired_untrusted_and_wrong_credentials(
    tmp_path: Path,
) -> None:
    with local_rnds(tmp_path) as server:
        endpoints = RndsEndpoints(
            "localhost", "localhost", server.auth_port, server.ehr_port
        )
        now = datetime.now(UTC)
        expired = issue(
            "Expired Establishment",
            issuer=server.ca,
            not_before=now - timedelta(days=60),
            not_after=now - timedelta(days=1),
        )
        foreign_ca = issue("Untrusted Root", ca=True)
        untrusted = issue("Untrusted Establishment", issuer=foreign_ca)
        valid = issue("Synthetic Establishment", issuer=server.ca)
        cases = (
            RndsCredential(pkcs12_bundle(expired, server.ca, b"um"), b"um"),
            RndsCredential(pkcs12_bundle(untrusted, foreign_ca, b"dois"), b"dois"),
            RndsCredential(pkcs12_bundle(valid, server.ca, b"tres"), b"errada"),
            RndsCredential(b"not a pkcs12 bundle", b"tres"),
        )
        for credential in cases:
            transport = MtlsRndsTransport(
                endpoints=endpoints, credential=credential, cafile=str(server.cafile)
            )
            with pytest.raises(RndsTransportError) as caught:
                transport.submit_bundle(body=b"{}", requester_cns="700000000000005")
            assert caught.value.kind == "credential"
        assert server.recorded.tokens == 0
        assert server.recorded.bundles == []
        closed = RndsEndpoints("localhost", "localhost", server.ehr_port, 1)
        transport = MtlsRndsTransport(
            endpoints=closed,
            credential=RndsCredential(pkcs12_bundle(valid, server.ca, b"q"), b"q"),
            cafile=str(server.cafile),
        )
        with pytest.raises(RndsTransportError):
            transport.submit_bundle(body=b"{}", requester_cns="700000000000005")


def test_credential_store_reads_only_owner_only_bounded_files(
    synthetic_secret_backend: Path,
) -> None:
    store = credential_store()
    good = synthetic_secret_backend / "rnds-sintetico.credential"
    good.touch(mode=0o600)
    good.write_bytes(b"\x30\x82bundle")
    assert store.get_credential("rnds-sintetico") == b"\x30\x82bundle"
    loose = synthetic_secret_backend / "rnds-aberto.credential"
    loose.write_bytes(b"bundle")
    loose.chmod(0o644)
    large = synthetic_secret_backend / "rnds-grande.credential"
    large.touch(mode=0o600)
    large.write_bytes(b"x" * 65537)
    empty = synthetic_secret_backend / "rnds-vazio.credential"
    empty.touch(mode=0o600)
    link = synthetic_secret_backend / "rnds-link.credential"
    link.symlink_to(good)
    for name in (
        "rnds-aberto",
        "rnds-grande",
        "rnds-vazio",
        "rnds-link",
        "ausente",
        "../x",
    ):
        with pytest.raises(SecretUnavailableError):
            store.get_credential(name)
    with pytest.raises(RndsTransportError) as caught:
        load_credential(uuid4())
    assert caught.value.kind == "credential"
