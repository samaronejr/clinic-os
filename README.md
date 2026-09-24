# Clinic OS

A Django 5.2 + PostgreSQL 16 staff-operated clinic management system: patient
registration/search, practitioner availability, booking, agendas, clinical
records, intake questionnaires, consent, attachments, synthetic prescribing,
synthetic PIX billing, reminders, retention, and teleconsult rehearsal —
behind audited, role-scoped staff and patient sessions with FORCE RLS tenant
isolation. All external integrations (payments, signing, video, messaging)
are explicitly synthetic and non-operational; real patient data and real
providers stay closed by design.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the runtime shape,
[docs/SECURITY.md](docs/SECURITY.md) for trust boundaries,
[docs/RUNBOOK.md](docs/RUNBOOK.md) for operations, and
[docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for change rules. The visual
language is specified in [DESIGN.md](DESIGN.md). Release-candidate evidence
lives in [docs/releases/](docs/releases/).

## Layout

- `apps/` — the 14 registered domain apps (`audit`, `billing`, `comms`,
  `consent`, `core`, `ehr`, `identity`, `intake`, `interop`, `prescription`,
  `retention`, `scheduling`, `teleconsult`, `tenancy`)
- `config/` — Django project: settings split per environment, URLconf, WSGI /
  ASGI / Celery entrypoints
- `templates/`, `static/` — server-rendered screens and self-hosted assets
- `tests/` — pytest suite, grouped by domain:
  - `tests/auth/` — authentication, TOTP/2FA, step-up verification
  - `tests/identity/` — users, organizations, RBAC, tenant middleware/RLS
  - `tests/patients/` — intake and patient registration/search
  - `tests/scheduling/` — availability, appointments, agendas, booking
  - `tests/ehr/` — clinical records and EHR migrations
  - `tests/audit/` — audit ledger and Phase 1A contracts
  - `tests/browser/` — recorded browser-session contracts
  - `tests/renewal/` — renewal release-candidate application tests
    (`tests/renewal/browser/` for the registered browser suites)
  - `tests/isolation/` — Phase 1A evidence-isolation and gate machinery
  - `tests/infra/` — settings, database, lifecycle, and schema gates
  - `tests/fixtures/` — shared test data (normative isolation fixtures)
  - shared harnesses (`otp_test_support`, `rbac_fixtures`, `database_urls`,
    `patient_service_support`, `patient_http_support`, `accessible_document`,
    `isolation_claim_fixtures`, `isolation_probe_fixtures`,
    `tenant_key_support`) stay at the `tests/` root; each domain keeps its
    own helpers beside its tests
- `ops/` — operations tooling: `ops/db/` database bootstrap/posture,
  `ops/container/` image runtime, `ops/testing/` CI, evidence, and gate
  harnesses
- `docs/` — architecture, security, runbook, contributing, compliance,
  release notes, and the hash-frozen approved plans in `docs/plans/`
- `terraform/` — infrastructure definitions (no live deployment yet)
- Root files — `manage.py`, `pyproject.toml` (+ `uv.lock`), `Makefile`,
  `Dockerfile`, `docker-compose.yml`, `docs` entry points

## Develop

```bash
uv sync --locked --all-groups
uv run pre-commit install
export RUNNER_TEMP="${RUNNER_TEMP:-$(mktemp -d)}"   # ci_postgres.sh requires it (set by GitHub Actions in CI)
ops/testing/ci_postgres.sh up                      # claimed PostgreSQL for the test suite
source "$RUNNER_TEMP/clinic-phase1a-ci-postgres/ci-postgres.env"
make db-bootstrap migrate db-posture
uv run pytest --reuse-db tests
```

The full local gate is `make ci` (ruff, ruff format, mypy --strict, pytest with
coverage, pip-audit, image/browser/TLS contract checks). Pre-commit runs ruff
and mypy on every commit. See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for
scoped test invocations and change rules.

## Test layout conventions

- `tests/` itself is not a package: files at its root are top-level modules,
  so root-level basenames must stay unique.
- Each domain directory is a package. Tests there import helpers as
  `domain.helper_module` (e.g. `scheduling.appointment_service_support`,
  `auth.stepup_test_support`); shared harnesses at the `tests/` root stay
  imported bare.
