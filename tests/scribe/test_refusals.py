"""Census HTTP refusals against hashes of every real migrated schema table."""

from __future__ import annotations

import base64
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.consent.services import publish_text, revoke_consent
from apps.intake.patient_access import patient_session_context
from apps.scribe import ingest
from apps.scribe.models import CaptureEpoch, RecordingSession
from apps.scribe.services import issue_encounter_token
from apps.scribe.views import CONTROL_ACTIONS
from apps.tenancy.db import tenant_context
from django.db import connection
from psycopg import sql
from psycopg.conninfo import make_conninfo

from rbac_fixtures import RBAC_RAW_CREDENTIAL
from renewal.test_encounters import physician_client
from scribe.test_ingest import BODY, CODEC, World, media, world

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)
ACTIONS = (*CONTROL_ACTIONS, "chunk")


@contextmanager
def fixture_connection(
    *,
    owner: bool = False,
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """Use only the owning test database; credentials never enter a receipt."""
    variable = "MIGRATION_DATABASE_URL" if owner else "TEST_SUPERUSER_DATABASE_URL"
    dsn = make_conninfo(
        os.environ[variable], dbname=str(connection.settings_dict["NAME"])
    )
    with psycopg.connect(dsn) as database:
        yield database


def snapshot() -> dict[str, str]:
    """Read opaque row hashes as the fixture observer, never as clinic_app."""
    with fixture_connection() as database:
        tables = database.execute(
            "SELECT c.relname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND c.relkind IN ('r','p','m') "
            "ORDER BY c.relname"
        ).fetchall()
        result = {}
        for (table,) in tables:
            assert isinstance(table, str)
            query = sql.SQL(
                "SELECT coalesce(jsonb_agg(to_jsonb(item) "
                "ORDER BY to_jsonb(item)::text),'[]'::jsonb)::text "
                "FROM {} item"
            ).format(sql.Identifier("clinic_app", table))
            row = database.execute(query).fetchone()
            assert row is not None
            assert isinstance(row[0], str)
            result[table] = sha256(row[0].encode()).hexdigest()
    return result


@dataclass(frozen=True, slots=True)
class API:
    graph: RbacGraph
    scope: World
    client: Client = field(repr=False)

    def post(
        self,
        command: str,
        *,
        clinic: UUID | None = None,
        token: str | None = None,
        changes: Mapping[str, object] | None = None,
    ) -> _MonkeyPatchedWSGIResponse:
        """Send the same body-only shape the real capture client sends."""
        selected = self.graph.clinic_a if clinic is None else clinic
        binding = self.scope.token if token is None else token
        if command == "chunk":
            payload = {
                "session_id": str(self.scope.session),
                "epoch": str(self.scope.epoch),
                "track": "microphone",
                "seq": 0,
                "sha256": sha256(BODY).hexdigest(),
                "duration_ms": 2000,
                "codec": CODEC,
                "bytes": base64.b64encode(BODY).decode(),
                **(changes or {}),
            }
            return self.client.post(
                f"/clinics/{selected}/scribe/chunk/",
                json.dumps(payload),
                "application/json",
                HTTP_X_ENCOUNTER_TOKEN=binding,
            )
        return self.client.post(
            f"/clinics/{selected}/scribe/control/",
            json.dumps(
                {
                    "action": command,
                    "encounter_token": binding,
                    "command_id": str(uuid4()),
                    "session_id": str(self.scope.session),
                    "expected_epoch": str(self.scope.epoch),
                    **(changes or {}),
                }
            ),
            "application/json",
        )


@contextmanager
def capture_api(graph: RbacGraph) -> Iterator[API]:
    """Open the same real authenticated capture client for fixture and census."""
    scope = world(graph)
    with physician_client(graph) as client:
        session = client.session
        with tenant_context(graph.physician, graph.organization_a):
            recording = RecordingSession.objects.get(pk=scope.session)
            session[f"workspace.patient.{graph.clinic_a}"] = str(
                recording.enrollment_id
            )
            session[f"ehr.encounter.{graph.clinic_a}"] = str(scope.encounter)
            assert session.session_key is not None
            token = issue_encounter_token(
                clinic_id=graph.clinic_a,
                encounter_id=scope.encounter,
                browser_binding=session.session_key,
            )
        session.save()
        value = API(graph, replace(scope, token=token), client)
        recovered = value.post("recover")
        assert recovered.status_code == 200
        resumed = value.post("resume")
        assert resumed.status_code == 200
        yield replace(
            value, scope=replace(value.scope, epoch=UUID(resumed.json()["epoch"]))
        )


@pytest.fixture
def api(rbac_graph: RbacGraph) -> Iterator[API]:
    with capture_api(rbac_graph) as value:
        yield value


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("tamper", ["token", "clinic", "patient"])
def test_every_action_refuses_stale_authority_without_any_row_write(
    api: API,
    action: str,
    tamper: str,
) -> None:
    if tamper == "patient":
        session = api.client.session
        session[f"workspace.patient.{api.graph.clinic_a}"] = str(uuid4())
        session.save()
    before = snapshot()
    response = api.post(
        action,
        token="invalid-signature" if tamper == "token" else None,
        clinic=api.graph.clinic_c if tamper == "clinic" else None,
    )
    assert response.status_code == 403
    assert snapshot() == before


@pytest.mark.parametrize("action", ACTIONS)
def test_role_loss_refuses_every_action_without_session_or_domain_mutation(
    api: API,
    action: str,
) -> None:
    with fixture_connection(owner=True) as database:
        row = database.execute(
            "SELECT username FROM clinic_app.identity_user WHERE id=%s",
            [api.graph.physician],
        ).fetchone()
        assert row is not None
        username = row[0]
        assert isinstance(username, str)
        database.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [str(api.graph.organization_a)],
        )
        database.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id,organization_id,clinic_id,user_id,role,workflow_bundle_version) "
            "VALUES (%s,%s,%s,%s,'receptionist',1) ON CONFLICT DO NOTHING",
            [
                uuid4(),
                api.graph.organization_a,
                api.graph.clinic_a,
                api.graph.physician,
            ],
        )
        database.execute(
            "UPDATE clinic_app.identity_userclinicrole SET revoked_at=now() "
            "WHERE user_id=%s AND clinic_id=%s AND role='physician'",
            [api.graph.physician, api.graph.clinic_a],
        )
    # The identity contract revokes the old authenticated session on a role
    # change. Reauthenticate the surviving receptionist membership, then
    # observe the capture refusal rather than the earlier login redirect.
    relogged = api.client.post(
        "/auth/login/",
        {"username": username, "password": RBAC_RAW_CREDENTIAL},
    )
    assert relogged.status_code == 302
    before = snapshot()
    response = api.post(action)
    assert response.status_code == 403
    assert snapshot() == before


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"sha256": "0" * 64}, "invalid_chunk"),
        ({"bytes": "!not-base64!"}, "invalid_chunk"),
        ({"bytes": ""}, "invalid_chunk"),
        (
            {"bytes": base64.b64encode(b"x" * (256 * 1024 + 1)).decode()},
            "invalid_chunk",
        ),
        ({"seq": -1}, "invalid_chunk"),
        ({"seq": 10799}, "invalid_chunk"),
        ({"duration_ms": 5001}, "invalid_chunk"),
        ({"codec": "audio/mpeg"}, "invalid_chunk"),
        ({"track": "patient-selector"}, "invalid_chunk"),
    ],
)
def test_invalid_upload_census_preserves_every_table(
    api: API,
    changes: dict[str, object],
    code: str,
) -> None:
    before = snapshot()
    response = api.post("chunk", changes=changes)
    assert (response.status_code, response.json()["code"]) == (400, code)
    assert snapshot() == before


@pytest.mark.parametrize("field", ["session_id", "epoch"])
def test_unknown_and_foreign_record_references_never_write(
    api: API,
    field: str,
) -> None:
    before = snapshot()
    response = api.post("chunk", changes={field: str(uuid4())})
    assert response.status_code in {403, 409}
    assert response.json()["code"] in {"access_denied", "epoch_stale"}
    assert response.json()["ack"] == "rejected"
    assert snapshot() == before


def test_epoch_rollback_and_paused_upload_are_read_only(api: API) -> None:
    with tenant_context(api.graph.physician, api.graph.organization_a):
        old = CaptureEpoch.objects.get(session_id=api.scope.session, number=1)
    before = snapshot()
    stale = api.post("pause", changes={"expected_epoch": str(old.pk)})
    assert (stale.status_code, stale.json()["code"]) == (409, "epoch_stale")
    assert snapshot() == before
    assert api.post("pause").status_code == 200
    paused = snapshot()
    refused = api.post("chunk")
    assert refused.status_code == 409
    assert refused.json() == {"ack": "rejected", "code": "session_not_recording"}
    assert snapshot() == paused


def test_expired_upload_cannot_reset_its_window(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tenant_context(api.graph.physician, api.graph.organization_a):
        epoch = CaptureEpoch.objects.get(pk=api.scope.epoch)
    monkeypatch.setattr(
        ingest, "utc_now", lambda: epoch.started_at + timedelta(seconds=70)
    )
    before = snapshot()
    response = api.post("chunk")
    assert response.status_code == 409
    assert response.json() == {"ack": "rejected", "code": "chunk_expired"}
    assert snapshot() == before


@pytest.mark.parametrize("failure", ["disabled", "remote_dev", "missing_dev_secrets"])
def test_storage_disable_does_not_fall_back_or_write(
    api: API,
    settings: SettingsWrapper,
    failure: str,
) -> None:
    if failure == "disabled":
        settings.SCRIBE_SYNTHETIC_CAPTURE = False
    else:
        settings.SCRIBE_DEV_MINIO = True
        settings.SCRIBE_S3_ENDPOINT = (
            "https://storage.example.invalid"
            if failure == "remote_dev"
            else "http://127.0.0.1:19040"
        )
    before = snapshot()
    response = api.post("chunk")
    assert (response.status_code, response.json()["code"]) == (
        409,
        "storage_unavailable",
    )
    assert snapshot() == before


@pytest.mark.parametrize("change", ["withdrawn", "superseded"])
def test_consent_loss_never_accepts_another_slice_or_mutates_on_refusal(
    api: API,
    change: str,
) -> None:
    if change == "withdrawn":
        with patient_session_context(api.scope.patient_session):
            revoke_consent(acceptance_id=api.scope.acceptance)
    else:
        with tenant_context(api.graph.clinic_admin, api.graph.organization_a):
            publish_text(
                clinic_id=api.graph.clinic_a,
                purpose="consultation_recording",
                text="Nova versão sintética exige nova autorização.",
            )
    before = snapshot()
    response = api.post("chunk")
    assert response.json() == {"ack": "rejected", "code": "consent_revoked"}
    assert snapshot() == before


def test_cross_session_replay_cannot_retarget_a_chunk(api: API) -> None:
    assert api.post("stop").status_code == 200
    authorized = api.post("authorize")
    assert authorized.status_code == 200
    identifier = authorized.json()["session_id"]
    started = api.post(
        "start", changes={"session_id": identifier, "expected_epoch": None}
    )
    assert started.status_code == 200
    before = snapshot()
    old_session = api.post("chunk")
    foreign_epoch = api.post("chunk", changes={"session_id": identifier})
    assert old_session.json() == {"ack": "rejected", "code": "session_not_recording"}
    assert foreign_epoch.json() == {"ack": "rejected", "code": "epoch_stale"}
    assert snapshot() == before


def test_command_key_reuse_cannot_change_the_recorded_operation(api: API) -> None:
    key = str(uuid4())
    assert api.post("pause", changes={"command_id": key}).status_code == 200
    before = snapshot()
    response = api.post("stop", changes={"command_id": key})
    assert (response.status_code, response.json()["code"]) == (
        409,
        "idempotency_mismatch",
    )
    assert snapshot() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "not-an-action"},
        {"command_id": "not-a-uuid"},
        {"expected_epoch": "not-an-epoch"},
        {"reason": "patient-identifying-reason"},
        {"last_seq": 10800},
        {"session_id": None},
    ],
)
def test_invalid_control_census_preserves_every_table(
    api: API,
    changes: dict[str, object],
) -> None:
    before = snapshot()
    response = api.post("status", changes=changes)
    assert (response.status_code, response.json()["code"]) == (400, "invalid_command")
    assert snapshot() == before
