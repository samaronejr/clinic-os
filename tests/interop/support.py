"""Shared interop test support: synthetic import worlds and partner fixtures.

No authority or storage is mocked: import worlds upload real sources, and partner
fixtures provision platform secrets, org admins, clients and tokens.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import secrets
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.interop import ratelimit, services, tasks
from apps.interop.management import IssuedCredential, create_client
from apps.interop.models import ImportBatch
from apps.interop.ratelimit import Decision
from apps.interop.webhook_transport import SYNTHETIC_TRANSPORT
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.test import Client

from auth.stepup_test_support import create_role_actor
from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

PATIENT_HEADER: Final = (
    "Codigo",
    "Nome",
    "Nome social",
    "Nascimento",
    "CPF",
    "Sexo",
    "Cadastrado em",
    "Cadastrado por",
)
HISTORY_HEADER: Final = (
    "Registro",
    "Paciente",
    "Tipo",
    "Data",
    "Profissional",
    "Texto",
    "Medida",
    "Valor",
    "Unidade",
    "Arquivo",
    "Titulo",
)
PATIENT_COLUMNS: Final = {
    "source_id": "Codigo",
    "legal_name": "Nome",
    "social_name": "Nome social",
    "birth_date": "Nascimento",
    "cpf": "CPF",
    "sex_at_birth": "Sexo",
    "registered_at": "Cadastrado em",
    "registered_by": "Cadastrado por",
}
HISTORY_COLUMNS: Final = {
    "source_id": "Registro",
    "patient_source_id": "Paciente",
    "kind": "Tipo",
    "recorded_at": "Data",
    "author": "Profissional",
    "text": "Texto",
    "code": "Medida",
    "value": "Valor",
    "unit": "Unidade",
    "file": "Arquivo",
    "title": "Titulo",
}
SYNTHETIC_PDF: Final = b"%PDF-1.4\n% Sintetico laudo importado\n%%EOF\n"


def part(document: Mapping[str, object], *keys: str | int) -> Any:  # noqa: ANN401
    """Read one nested report value; reports are closed JSON documents."""
    value: Any = document
    for key in keys:
        value = value[key]
    return value


@dataclass(frozen=True, slots=True)
class ImportWorld:
    """One organization with an import operator holding a fresh step-up session."""

    graph: RbacGraph
    operator: UUID
    session: str


def import_world(graph: RbacGraph, role: str = "clinic_manager") -> ImportWorld:
    """Create an operator at clinic A and verify its session over HTTP."""
    actor = create_role_actor(graph, UserClinicRole.Role(role))
    key = session_key(verified_client(replace(graph, clinic_admin=actor.pk)))
    return ImportWorld(graph=graph, operator=actor.pk, session=key)


@contextmanager
def operating(world: ImportWorld, actor: UUID | None = None) -> Iterator[None]:
    """Run as the real runtime role inside the actor's tenant transaction."""
    with (
        runtime_role(),
        tenant_context(actor or world.operator, world.graph.organization_a),
    ):
        yield


def cpf(number: int) -> str:
    """Return a check-digit-valid synthetic CPF for a positive number."""
    digits = [int(character) for character in f"{100000000 + number:09d}"[-9:]]
    for size in (9, 10):
        total = sum(
            digit * weight
            for digit, weight in zip(digits, range(size + 1, 1, -1), strict=True)
        )
        remainder = total % 11
        digits.append(0 if remainder < 2 else 11 - remainder)
    return "".join(str(digit) for digit in digits)


def patient_row(number: int, **overrides: str) -> list[str]:
    """One synthetic legacy-system patient row."""
    birth = date(1950, 1, 1) + timedelta(days=number % 20000)
    values = {
        "Codigo": f"SIN-{number:06d}",
        "Nome": f"Paciente Sintetico {number:06d}",
        "Nome social": "",
        "Nascimento": birth.strftime("%d/%m/%Y"),
        "CPF": cpf(number),
        "Sexo": "F" if number % 2 else "M",
        "Cadastrado em": "05/03/2019 09:30",
        "Cadastrado por": "Recepcao Sintetica Legada",
        **overrides,
    }
    return [values[column] for column in PATIENT_HEADER]


def csv_bytes(
    header: Sequence[str], rows: Iterable[Sequence[str]], delimiter: str = ";"
) -> bytes:
    """Encode rows the way a spreadsheet export would (UTF-8 with BOM)."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def patients_csv(count: int, *, extra: Iterable[Sequence[str]] = ()) -> bytes:
    """A semicolon-delimited export of ``count`` valid patients plus extras."""
    return csv_bytes(
        PATIENT_HEADER,
        [*(patient_row(number) for number in range(1, count + 1)), *extra],
    )


def mapping(source_kind: str = "csv", *, history: bool = False) -> dict[str, object]:
    """The operator's mapping for the synthetic legacy layout."""
    document: dict[str, object] = {
        "schema": "import-mapping-v1",
        "source_kind": source_kind,
        "date_format": "DD/MM/YYYY",
        "datetime_format": "DD/MM/YYYY HH:MM",
        "datasets": {
            "patients": {
                "columns": dict(PATIENT_COLUMNS),
                "values": {"sex_at_birth": {"F": "female", "M": "male"}},
            },
            **(
                {
                    "history": {
                        "columns": dict(HISTORY_COLUMNS),
                        "values": {
                            "kind": {
                                "Evolucao": "note",
                                "Sinal vital": "observation",
                                "Anexo": "attachment",
                            }
                        },
                    }
                }
                if history
                else {}
            ),
        },
    }
    if source_kind in ("csv", "zip"):
        options = {"delimiter": ";", "encoding": "utf-8"}
        document["csv"] = {
            "patients": options,
            **({"history": options} if history else {}),
        }
    return document


def archive(
    members: dict[str, bytes], *, manifest_override: dict[str, object] | None = None
) -> bytes:
    """Build a clinic-import-archive-v1 ZIP with an exact checksum manifest."""
    manifest = manifest_override or {
        "v": "clinic-import-archive-v1",
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            for name, data in sorted(members.items())
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr("manifest.json", json.dumps(manifest))
        for name, data in sorted(members.items()):
            target.writestr(name, data)
    return buffer.getvalue()


def history_csv(rows: Iterable[Sequence[str]]) -> bytes:
    """A clinical history export bound to patients by legacy codes."""
    return csv_bytes(HISTORY_HEADER, rows)


def history_row(  # noqa: PLR0913 - one row of the synthetic legacy layout
    source: str,
    patient: str,
    kind: str,
    *,
    text: str = "",
    measure: tuple[str, str, str] = ("", "", ""),
    file: str = "",
    author: str = "Dra. Sintetica Origem",
    when: str = "12/02/2020 14:05",
) -> list[str]:
    """One synthetic history row: note, vital sign or document reference."""
    code, value, unit = measure
    return [source, patient, kind, when, author, text, code, value, unit, file, ""]


def upload(
    world: ImportWorld, data: bytes, *, kind: str = "csv"
) -> services.ImportJobView:
    """Upload as the operator with the exact declared checksum."""
    with operating(world):
        return services.upload_import(
            clinic_id=world.graph.clinic_a,
            source_kind=kind,
            data=data,
            declared_sha256=hashlib.sha256(data).hexdigest(),
            idempotency_key=uuid4(),
        )


def to_dry_run(
    world: ImportWorld,
    data: bytes,
    *,
    kind: str = "csv",
    document: dict[str, object] | None = None,
) -> services.DryRunReport:
    """Upload, profile, save + apply a mapping and dry-run one source."""
    job = upload(world, data, kind=kind)
    with operating(world):
        profiled = services.profile_import(
            clinic_id=world.graph.clinic_a, job_id=job.job_id, expected_sequence=1
        )
        saved = services.save_mapping(
            clinic_id=world.graph.clinic_a,
            code=f"legado-{uuid4().hex[:8]}",
            document=document or mapping(kind, history=kind == "zip"),
        )
        mapped = services.apply_mapping(
            clinic_id=world.graph.clinic_a,
            job_id=job.job_id,
            mapping_id=saved.mapping_id,
            expected_sequence=profiled.job.sequence,
        )
        return services.dry_run_import(
            clinic_id=world.graph.clinic_a,
            job_id=job.job_id,
            expected_sequence=mapped.sequence,
        )


def approve(
    world: ImportWorld, report: services.DryRunReport
) -> services.ImportJobView:
    """Sign off the cutover plan, acknowledging every reported issue."""
    with operating(world):
        return services.approve_import(
            clinic_id=world.graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=report.job.sequence,
            plan=services.CutoverPlan(
                rollback_window_hours=72,
                source_frozen=True,
                backup_verified=True,
                rejects_acknowledged=True,
                duplicates_acknowledged=True,
            ),
        )


def run_batches(
    world: ImportWorld, job_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Deliver every pending batch to the real bulk task, in order, until done."""
    monkeypatch.setattr(tasks, "fair_acquire", lambda **_kwargs: True)
    monkeypatch.setattr(tasks.commit_batch_job, "apply_async", lambda **_kwargs: None)
    outcomes: list[str] = []
    with runtime_role(), tenant_context(world.operator, world.graph.organization_a):
        pending = list(
            ImportBatch.objects.filter(job_id=job_id, state="pending")
            .order_by("sequence")
            .values_list("pk", flat=True)
        )
    for batch_id in pending:
        with runtime_role():
            outcomes.append(tasks.commit_batch(batch_id=str(batch_id)))
    return outcomes


def committed(
    world: ImportWorld,
    data: bytes,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str = "csv",
    document: dict[str, object] | None = None,
) -> tuple[services.DryRunReport, services.CommitReceipt]:
    """Take one source from upload to a fully committed import."""
    report = to_dry_run(world, data, kind=kind, document=document)
    approved = approve(world, report)
    with operating(world):
        receipt = services.commit_import(
            clinic_id=world.graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    outcomes = run_batches(world, report.job.job_id, monkeypatch)
    assert outcomes == ["committed"] * len(receipt.batch_ids), outcomes
    return report, receipt


ALL_SCOPES = (
    "appointments:read",
    "availability:read",
    "invoices.status:read",
    "patients.demographics:read",
    "webhooks:manage",
)
PLATFORM_SECRETS = ("interop-external-ids", "interop-webhook-signing")


@pytest.fixture
def interop_secrets(synthetic_secret_backend: Path) -> Path:
    """Provision the external-id and webhook-signing platform secrets."""
    for name in PLATFORM_SECRETS:
        path = synthetic_secret_backend / f"{name}.secret"
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return synthetic_secret_backend


@dataclass
class LocalBucket:
    """The production token-bucket arithmetic in process, under a frozen clock.

    Gated tests prove the same arithmetic in the Redis Lua script; this keeps
    every other API test independent of a broker while metering stays real.
    """

    now: float = 1_700_000_000.0
    buckets: dict[str, tuple[float, float]] = field(default_factory=dict)
    unavailable: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def spend(self, *, client_id: UUID, per_minute: int, purpose: str) -> Decision:
        if self.unavailable:
            raise ratelimit.RateLimitUnavailableError
        key = f"{purpose}:{client_id}"
        rate = per_minute / 60.0
        with self._lock:
            tokens, touched = self.buckets.get(key, (float(per_minute), self.now))
            tokens = min(per_minute, tokens + max(0.0, self.now - touched) * rate)
            if tokens >= 1:
                self.buckets[key] = (tokens - 1, self.now)
                return Decision(allowed=True, retry_after_seconds=0)
            self.buckets[key] = (tokens, self.now)
            wait = math.ceil((1 - tokens) / rate * 1000)
            return Decision(
                allowed=False, retry_after_seconds=max(1, math.ceil(wait / 1000))
            )


@pytest.fixture
def local_bucket(monkeypatch: pytest.MonkeyPatch) -> LocalBucket:
    """Install the in-process bucket for API tests that do not prove Redis."""
    bucket = LocalBucket()
    monkeypatch.setattr(ratelimit, "_backend", bucket)
    return bucket


@pytest.fixture
def webhook_transport() -> Iterator[None]:
    """Start and finish every test with an empty synthetic transport."""
    SYNTHETIC_TRANSPORT.reset()
    yield
    SYNTHETIC_TRANSPORT.reset()


def org_admin(graph: RbacGraph, role: str = "org_admin") -> UUID:
    """Create an administrator holding ``role`` on every clinic of organization A."""
    user = create_role_actor(graph, UserClinicRole.Role(role))
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user=user,
            role=role,
        )
    return user.pk


def org_b_admin(graph: RbacGraph) -> UUID:
    """Create an administrator of organization B (its only clinic C)."""
    user = User.objects.create(
        username=f"sintetico-org-b-admin-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
    )
    with owner_context(graph.organization_b):
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user=user,
            role="org_admin",
        )
    return user.pk


def make_client(  # noqa: PLR0913 - one partner registration's terms
    graph: RbacGraph,
    admin: UUID,
    scopes: Sequence[str] = ALL_SCOPES,
    *,
    organization: UUID | None = None,
    clinic: UUID | None = None,
    rate: int = 600,
) -> IssuedCredential:
    """Register a partner client through the real management service."""
    with runtime_role(), tenant_context(admin, organization or graph.organization_a):
        return create_client(
            clinic_id=clinic or graph.clinic_a,
            name="Parceiro Sintetico",
            scopes=list(scopes),
            rate_limit_per_minute=rate,
            command_id=uuid4(),
        )


def basic(credential: IssuedCredential) -> str:
    raw = f"{credential.client_id}:{credential.client_secret}".encode("ascii")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def request_token(
    http: Client, credential: IssuedCredential, scope: str | None = None
) -> _MonkeyPatchedWSGIResponse:
    data = {"grant_type": "client_credentials"}
    if scope is not None:
        data["scope"] = scope
    return token_post(http, data, authorization=basic(credential))


def token_post(
    http: Client, data: dict[str, str], *, authorization: str | None = None
) -> _MonkeyPatchedWSGIResponse:
    """POST one RFC 6749 form-encoded token request."""
    headers = {} if authorization is None else {"Authorization": authorization}
    with runtime_role():
        return http.post(
            "/api/v1/oauth/token",
            urlencode(data),
            content_type="application/x-www-form-urlencoded",
            headers=headers,
        )


def access_token(credential: IssuedCredential, scope: str | None = None) -> str:
    response = request_token(Client(), credential, scope)
    assert response.status_code == 200, response.content
    token = response.json()["access_token"]
    assert isinstance(token, str)
    return token


def api_get(http: Client, token: str, path: str) -> _MonkeyPatchedWSGIResponse:
    with runtime_role():
        return http.get(path, HTTP_AUTHORIZATION=f"Bearer {token}")


def api_post(
    http: Client,
    token: str,
    path: str,
    body: object,
    *,
    key: str | None = None,
) -> _MonkeyPatchedWSGIResponse:
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    with runtime_role():
        return http.post(path, body, content_type="application/json", headers=headers)
