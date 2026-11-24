"""Reference clinic, personas and the flagship report of the todo 75 suites.

Every flagship suite (the seven journeys and ``acceptance-scenarios``) runs on
one reference clinic shaped by D-17: two new units of the runner's
organization (the runner's bootstrap clinic stays with the reused per-feature
proofs, whose assertions own it), four rooms (two per unit) and twelve
clinicians, plus the reception, nurse, management, finance and organization
administration personas. Only these fixture identities and rooms are seeded
through the owner DSN; every clinical, scheduling and payment record a journey
asserts is created through the product's own screens or authenticated
endpoints, served as ``clinic_app``.

Each suite records into one ``FlagshipReport`` (conftest fixture
``flagship``): the adapters it touched, labelled ``real`` or ``synthetic``;
every provider leg asserted ``BLOCKED-ON-EG`` from the capability registry as
the runtime role reads it (``blocked_on_eg``); the journey steps and scenario
rows it covered, each with the branch that proved it. The session writes
``<artifact root>/<suite>/flagship.json``; the runner binds it, together with
its own PHI-sentinel scan of every sink, into the suite's ``report.json``.

The report never holds a sentinel, a name or a record identifier: steps carry
fixed labels only, and ``write`` refuses a report that contains the sentinel
prefix.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import psycopg
from django.contrib.auth.hashers import make_password
from django_otp.oath import TOTP
from ops.testing.renewal_runner import FLAGSHIP_REPORT, SUITES
from ops.testing.sentinel_scan import SENTINEL_PREFIX

from renewal.browser._fixture_secrets import (
    FixtureSecret,
    fixture_dsn,
    lock_dsn,
    new_password,
    new_totp_key,
)
from renewal.browser._navigation import (
    click_to_navigate,
    goto_settled,
    wait_for_signed_in,
)
from renewal.browser._page_wait import await_autofocus
from renewal.browser._totp_devices import reset_totp_device, seed_totp_device

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page

REPOSITORY: Final = Path(__file__).resolve().parents[3]
CLINIC_ZONE: Final = "America/Sao_Paulo"
BROWSER_ZONE: Final = "Asia/Tokyo"
# DV/G1: desktop and mobile widths of every flagship run.
WIDTHS: Final = (1280, 375)
UNIT_A: Final = "Clínica Referência Unidade Paulista"
UNIT_B: Final = "Clínica Referência Unidade Moema"
ROOMS: Final = {
    "unit_a": ("Consultório 1", "Consultório 2"),
    "unit_b": ("Consultório 3", "Sala de procedimentos"),
}
CLINICIANS_PER_UNIT: Final = 6
# Personas that sign in without an authenticator (non-privileged front desk).
NO_TOTP_ROLES: Final = frozenset({"receptionist", "scheduler"})
# Roles whose sign-in the product always completes with a TOTP challenge.
CHALLENGED_ROLES: Final = frozenset(
    {"physician", "clinic_admin", "clinic_manager", "org_admin"}
)
LABELS: Final = frozenset({"real", "synthetic"})
USERNAME_STEMS: Final = {
    "physician": "medica",
    "nurse": "enfermeira",
    "receptionist": "recepcao",
    "clinic_admin": "administracao",
    "clinic_manager": "gestora",
    "finance": "financeiro",
    "org_admin": "rede",
}
BLOCKED: Final = "BLOCKED-ON-EG"
# Provider capability -> the external gates its live/sandbox leg waits on.
CAPABILITY_GATES: Final = {
    "asr": ("EG-1", "EG-2", "EG-3", "EG-5"),
    "llm_inference": ("EG-1", "EG-2", "EG-3", "EG-5"),
    "object_storage_media": ("EG-1", "EG-5"),
    "attachment_scanning": ("EG-1", "EG-5"),
    "email": ("EG-1", "EG-5"),
    "sms": ("EG-1", "EG-5"),
    "whatsapp": ("EG-1", "EG-5"),
    "video": ("EG-1", "EG-5"),
    "pix": ("EG-1", "EG-5"),
    "psp_card": ("EG-1", "EG-5"),
    "qualified_signing": ("EG-1", "EG-5"),
    "physician_registration": ("EG-17",),
    "sncr": ("EG-6",),
    "nfse": ("EG-9",),
    "tiss": ("EG-8",),
    "rnds": ("EG-7",),
    "hosted_pitr": ("EG-12",),
    "managed_secrets": ("EG-1", "EG-12"),
    "tenant_key_management": ("EG-1", "EG-12"),
    "saas_collection": ("EG-18",),
    # Not a provider capability: real competitor export formats (scenario 16).
    "competitor_import_formats": ("EG-15",),
}


@dataclass(frozen=True, slots=True)
class Persona:
    """One seeded staff identity of the reference clinic."""

    key: str
    username: str
    user_id: str
    role: str
    clinics: tuple[str, ...]


@dataclass(slots=True)
class ReferenceClinic:
    """The D-17 dataset shared by one flagship suite session."""

    dsn: FixtureSecret
    organization: str
    unit_a: str
    unit_b: str
    password: FixtureSecret
    personas: dict[str, Persona]
    rooms: dict[str, list[str]]
    tag: str

    def clinic(self, unit: str) -> str:
        """Return the clinic id of ``unit_a`` / ``unit_b``."""
        return self.unit_a if unit == "unit_a" else self.unit_b

    def clinicians(self, unit: str) -> list[Persona]:
        """Return the unit's clinicians (physicians and nurses), in seed order."""
        clinic = self.clinic(unit)
        return [
            persona
            for persona in self.personas.values()
            if persona.role in {"physician", "nurse"} and clinic in persona.clinics
        ]

    def owner_connection(
        self, actor: str | None = None
    ) -> psycopg.Connection[tuple[object, ...]]:
        """Open a fixture-owner connection scoped to the organization.

        ``actor`` (a persona key) sets ``app.current_user_id`` for writes whose
        triggers check the acting user's authority (scheduling definitions).
        """
        connection = psycopg.connect(self.dsn)
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, false)", [self.organization]
        )
        if actor is not None:
            connection.execute(
                "SELECT set_config('app.current_user_id', %s, false)",
                [self.personas[actor].user_id],
            )
        return connection


def _persona_specs(tag: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Return (key, role, units) for every seeded persona."""
    specs: list[tuple[str, str, tuple[str, ...]]] = []
    for unit in ("unit_a", "unit_b"):
        letter = unit[-1]
        for index in range(1, CLINICIANS_PER_UNIT + 1):
            # Five physicians and one nurse per unit: twelve clinicians.
            role = "nurse" if index == CLINICIANS_PER_UNIT else "physician"
            # physician_a2 also consults at unit B (a cross-unit clinician).
            units = ("unit_a", "unit_b") if (letter, index) == ("a", 2) else (unit,)
            specs.append((f"{role}_{letter}{index}", role, units))
    specs += [
        ("reception_a1", "receptionist", ("unit_a",)),
        ("reception_a2", "receptionist", ("unit_a",)),
        ("reception_b1", "receptionist", ("unit_b",)),
        ("admin_a", "clinic_admin", ("unit_a",)),
        ("manager_a", "clinic_manager", ("unit_a",)),
        ("finance_a", "finance", ("unit_a",)),
        ("org_admin", "org_admin", ("unit_a", "unit_b")),
    ]
    del tag
    return specs


def seed_reference_clinic() -> ReferenceClinic:
    """Seed the two units, four rooms and every persona; return the dataset."""
    tag = uuid4().hex[:5]
    reference = ReferenceClinic(
        dsn=fixture_dsn(),
        organization=os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        unit_a=str(uuid4()),
        unit_b=str(uuid4()),
        password=new_password(),
        personas={},
        rooms={"unit_a": [], "unit_b": []},
        tag=tag,
    )
    hashed = make_password(reference.password)
    with reference.owner_connection() as connection:
        for clinic, name in ((reference.unit_a, UNIT_A), (reference.unit_b, UNIT_B)):
            connection.execute(
                "INSERT INTO clinic_app.identity_clinic "
                "(id, organization_id, name, crm_uf, timezone) "
                "VALUES (%s, %s, %s, 'SP', %s)",
                [clinic, reference.organization, name, CLINIC_ZONE],
            )
        for key, role, units in _persona_specs(tag):
            user_id = str(uuid4())
            # Portuguese handles: usernames are shown, and English UI words
            # are banned from pt-BR screens (test_end_to_end.ENGLISH).
            username = f"ref-{USERNAME_STEMS[role]}-{key.rsplit('_', 1)[-1]}-{tag}"
            connection.execute(
                "INSERT INTO clinic_app.identity_user "
                "(id, username, password, email, first_name, last_name, is_active, "
                "is_staff, is_superuser, date_joined) "
                "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
                [user_id, username, hashed, f"{username}@reference.invalid"],
            )
            clinics = tuple(reference.clinic(unit) for unit in units)
            # Organization-wide authority needs a role on every clinic of the
            # organization, the runner's bootstrap clinic included.
            granted = (
                (*clinics, os.environ["CLINIC_RENEWAL_CLINIC_ID"])
                if role == "org_admin"
                else clinics
            )
            for clinic in granted:
                connection.execute(
                    "INSERT INTO clinic_app.identity_userclinicrole "
                    "(id, user_id, organization_id, clinic_id, role, "
                    "workflow_bundle_version) VALUES (%s, %s, %s, %s, %s, 1)",
                    [str(uuid4()), user_id, reference.organization, clinic, role],
                )
            if role not in NO_TOTP_ROLES:
                seed_totp_device(connection, user_id, new_totp_key())
            reference.personas[key] = Persona(key, username, user_id, role, clinics)
        # Scheduling definitions need an actor holding configuration authority.
        connection.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [reference.personas["org_admin"].user_id],
        )
        for unit, names in ROOMS.items():
            for name in names:
                room = str(uuid4())
                connection.execute(
                    "INSERT INTO clinic_app.scheduling_resource "
                    "(id, organization_id, clinic_id, kind, name, capacity, active) "
                    "VALUES (%s, %s, %s, 'room', %s, 1, true)",
                    [room, reference.organization, reference.clinic(unit), name],
                )
                reference.rooms[unit].append(room)
    return reference


def sign_in(page: Page, base_url: str, reference: ReferenceClinic, key: str) -> None:
    """Sign ``key`` in through the real login (and TOTP verify) screens."""
    persona = reference.personas[key]
    totp_key = None
    if persona.role not in NO_TOTP_ROLES:
        # A fresh authenticator seed per sign-in: no 30 s window to wait out.
        totp_key = new_totp_key()
        with psycopg.connect(reference.dsn) as connection:
            reset_totp_device(connection, persona.user_id, totp_key)
    goto_settled(page, f"{base_url}/auth/login/")
    await_autofocus(page.locator("#id_username"))
    page.locator("#id_username").fill(persona.username)
    page.locator("#id_password").fill(reference.password)
    click_to_navigate(page.locator("button[type=submit]"))
    # Privileged roles must be asked for their authenticator; others may not be.
    challenged = "/auth/verify/" in page.url
    assert challenged or persona.role not in CHALLENGED_ROLES, persona.role
    if totp_key is not None and challenged:
        token = TOTP(bytes.fromhex(totp_key), 30, 0, 6, 0).token()
        page.locator("#id_otp_token").fill(f"{token:06d}")
        click_to_navigate(page.locator("button[type=submit]"))
    wait_for_signed_in(page)


def staff_context(
    browser: Browser,
    width: int,
    **options: Any,  # noqa: ANN401 - Playwright options pass through
) -> BrowserContext:
    """Open one pt-BR context at ``width`` in a browser zone far from the clinic."""
    return browser.new_context(
        locale="pt-BR",
        timezone_id=BROWSER_ZONE,
        viewport={"width": width, "height": 900},
        **options,
    )


def capture(page: Page, root: Path, suite: str, name: str) -> str:
    """Save one viewport capture under ``<root>/<suite>/`` (0o600)."""
    directory = root / suite
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / f"{name}.png"
    page.screenshot(path=str(destination), full_page=False)
    destination.chmod(0o600)
    return destination.name


def capability_state(dsn: str, key: str, clinic_id: str) -> str:
    """Return the capability's current state as the runtime role reads it."""
    with psycopg.connect(dsn) as connection:
        connection.execute("SET ROLE clinic_app")
        row = connection.execute(
            "SELECT coalesce(v.state, 'unregistered') "
            "FROM clinic_app.providers_providercapability AS c "
            "LEFT JOIN clinic_app.providers_capabilityversion AS v "
            "ON v.id = c.current_version_id "
            "WHERE c.key = %s AND (c.clinic_id = %s OR c.clinic_id IS NULL) "
            "ORDER BY c.clinic_id IS NULL LIMIT 1",
            [key, clinic_id],
        ).fetchone()
    return "unregistered" if row is None else str(row[0])


@dataclass
class FlagshipReport:
    """What one flagship suite proved, written as ``flagship.json``."""

    suite: str
    adapters: dict[str, dict[str, object]] = field(default_factory=dict)
    blocked_on_eg: list[dict[str, object]] = field(default_factory=list)
    covers: list[dict[str, object]] = field(default_factory=list)
    sentinel_labels: list[str] = field(default_factory=list)

    def adapter(self, name: str, label: str, detail: str, **extra: object) -> None:
        """Label one adapter the suite touched (``real`` or ``synthetic``)."""
        assert label in LABELS, label
        self.adapters[name] = {"label": label, "detail": detail, **extra}

    def blocked(self, reference: ReferenceClinic, key: str, unit: str) -> str:
        """Assert the live/sandbox leg of ``key`` is unreachable; record it.

        The leg is blocked only when the registry, read as ``clinic_app``,
        holds no ``activated`` version for the unit and the suite runs in
        synthetic data mode (``providers.services.is_live`` needs both).
        """
        state = capability_state(reference.dsn, key, reference.clinic(unit))
        mode = os.environ.get("CLINIC_DATA_MODE")
        assert state != "activated", (key, state)
        assert mode == "synthetic", mode
        self.blocked_on_eg.append(
            {
                "capability": key,
                "gates": list(CAPABILITY_GATES.get(key, ("EG-1", "EG-5"))),
                "registry_state": state,
                "data_mode": mode,
                "status": BLOCKED,
            }
        )
        return state

    def cover(self, item: str, branch: str, **facts: object) -> None:
        """Record one journey step or scenario row and the branch that proved it."""
        self.covers.append({"item": item, "branch": branch, **facts})

    def sentinel(self, label: str) -> None:
        """Record that a sentinel of ``label`` was injected (label only)."""
        self.sentinel_labels.append(label)

    def document(self) -> dict[str, object]:
        """Return the JSON document of this report."""
        return {
            "schema_version": 1,
            "suite": self.suite,
            "evidence_level": "L1",
            "live_readiness": False,
            "adapters": dict(sorted(self.adapters.items())),
            "blocked_on_eg": self.blocked_on_eg,
            "covers": self.covers,
            "sentinel_labels": sorted(set(self.sentinel_labels)),
        }

    def write(self, root: Path) -> Path:
        """Write ``<root>/<suite>/flagship.json``; refuse sentinel content."""
        body = json.dumps(self.document(), sort_keys=True, indent=2) + "\n"
        assert SENTINEL_PREFIX not in body
        directory = root / self.suite
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / FLAGSHIP_REPORT
        destination.write_text(body, encoding="utf-8")
        destination.chmod(0o600)
        return destination


def run_fixture(
    dsn: str,
    action: str,
    data: dict[str, str],
    *,
    killed: bool = False,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    """Run one ``renewal.flagship_fixture`` domain-service step; return its JSON.

    ``killed`` expects the step's process to die by SIGKILL (a worker killed
    after a remote effect) and returns an empty result.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from renewal.flagship_fixture import main; main()",
        ],
        input=json.dumps({"action": action, **data}),
        env={
            **os.environ,
            "APP_DATABASE_URL": dsn,
            "LOCKS_DATABASE_URL": lock_dsn(),
            "DJANGO_SETTINGS_MODULE": "config.settings.base",
            "CLINIC_DATA_MODE": "synthetic",
            "BILLING_SYNTHETIC_PSP": "true",
            "BILLING_SYNTHETIC_PIX": "true",
            "SCRIBE_SYNTHETIC_CAPTURE": "1",
            "SCRIBE_MEDIA_ROOT": str(
                Path(os.environ["CLINIC_SECRET_DIR"]) / "flagship-media"
            ),
            "CELERY_BROKER_URL": "memory://",
            "PYTHONPATH": os.pathsep.join((".", "tests")),
            **(environment or {}),
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=180,
        cwd=REPOSITORY,
    )
    if killed:
        assert completed.returncode == -signal.SIGKILL, completed.stderr[-2000:]
        return {}
    assert completed.returncode == 0, completed.stderr[-4000:]
    result: dict[str, object] = json.loads(completed.stdout)
    return result


def suite_of(module: Path) -> str:
    """Return the registered suite that owns ``module``."""
    relative = module.resolve().relative_to(REPOSITORY).as_posix()
    owners = [name for name, paths in SUITES.items() if relative in paths]
    assert len(owners) == 1, relative
    return owners[0]
