# PROJECT KNOWLEDGE BASE

**Generated:** 2026-09-20
**Commit:** d70db17
**Branch:** detached HEAD (d70db17)

## OVERVIEW
Clinic OS is a Brazilian outpatient-clinic workspace using Python 3.12/3.13, Django 5.2, PostgreSQL 16, server-rendered templates, and HTMX.
Current scope is synthetic-data Phase 1A: identity, tenant isolation, audit, patient intake, and staff scheduling; live use remains blocked by external approvals.

## STRUCTURE
```text
clinic_project/
|-- apps/              # Product domains, shared core, and deferred service seams
|-- config/            # HTTP routing, runtime startup, explicit settings profiles
|-- templates/         # Domain screens; HTMX fragments in domain partials/
|-- static/            # First-party CSS/JS and locally vendored HTMX
|-- tests/             # Pytest discovery, cross-domain fixtures, normative vectors
|-- ops/               # Container/DB contracts and executable evidence tooling
|-- docs/              # Architecture, runbook, security, frozen plan, approval gates
|-- terraform/         # Validation-only infrastructure; not deployment authority
|-- .github/workflows/ # Quality and image/browser/TLS provenance checks
|-- DESIGN.md          # UI tokens, components, and interaction constraints
`-- Makefile           # DB lifecycle, owner commands, and CI orchestration
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Product behavior and module status | `apps/AGENTS.md` | Routes to domain guides; distinguishes working domains from stubs |
| Operator lifecycle commands | `apps/identity/management/AGENTS.md` | Owner-role command boundary |
| Request routing and probes | `config/urls.py`, `apps/core/views.py` | Shell, health/readiness, identity, intake, scheduling |
| Startup/settings contracts | `config/settings/AGENTS.md` | Runtime, release, browser authority, import-time gates |
| Test fixtures and database teardown | `tests/AGENTS.md` | Real PostgreSQL/RLS tests and cross-domain support |
| Container and database operations | `ops/AGENTS.md` | Bootstrap, posture, release contracts |
| Isolation ledger and evidence | `ops/testing/AGENTS.md` | Tooling, not the pytest discovery directory |
| Live browser journeys | `ops/testing/browser_suites/AGENTS.md` | Runner-owned executable suites |
| UI implementation | `templates/base.html`, `static/`, `DESIGN.md` | Domain templates, local assets, design contract |
| Architecture and trust boundaries | `docs/ARCHITECTURE.md`, `docs/SECURITY.md` | Implemented scope and authority model |
| Environment and local startup | `docs/RUNBOOK.md` | Configure roles/environment before Makefile DB commands |
| Change review and live-data approval | `docs/CONTRIBUTING.md`, `docs/compliance/LIVE-DATA-GATE.md` | Reviews under compliance are not approval |
| Infrastructure validation | `terraform/README.md` | PostgreSQL 16 RDS in sa-east-1; no authorized deployment |

## CODE MAP
Reference counts below are digest-reported static measurements, not runtime call counts.
`LSP` excludes declarations but includes imports/type uses; `AST` counts unqualified name uses, not resolved symbol identities.

| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `UserClinicRole` | Model | `apps/identity/models.py:87` | 249 LSP | Canonical clinic-role authority |
| `Appointment` | Model | `apps/scheduling/models.py:96` | 205 LSP | Appointment state and lifecycle constraints |
| `record_phase1_event` | Function | `apps/audit/services.py:197` | 43 LSP | Fixed-vocabulary audit append |
| `create_patient` | Function | `apps/intake/patient_creation.py:100` | 28 LSP | Idempotent patient/enrollment registration |
| `RbacGraph` | Fixture type | `tests/rbac_fixtures.py:15` | 402 LSP | Shared organization/clinic actor graph |
| `TenantGraph` | Fixture type | `tests/conftest.py:28` | 42 LSP | Tenant/GUC/backend regression graph |
| `database_url_for_name` | Function | `tests/database_urls.py:4` | 32 LSP | Retarget raw connections to the current test DB |
| `JsonObject` | Type alias | `ops/testing/isolation_common.py:27` | 1612 AST | Evidence/ledger object shape |
| `write_no_replace` | Function | `ops/testing/isolation_common.py:143` | 78 AST | Durable exclusive record publication |
| `canonical_sha256` | Function | `ops/testing/isolation_common.py:47` | 15 LSP | Shared authority/evidence hashing |
| `parse_database_url` | Function | `config/settings/database.py:15` | 8 LSP | Database role/TLS connection contract |
| `enforce_wheel_timezone` | Function | `config/runtime.py:13` | 6 LSP | Locked timezone source before app import |

## CONVENTIONS
- Product services and operational tooling are separate surfaces: `apps/` owns business behavior; `ops/testing/` ships runner contracts consumed by `tests/`.
- HTTP runtime uses `clinic_app`; migration/owner lifecycle uses `clinic_owner`. Follow the app/settings guides for transaction-bound authority; ownership alone is not operator authorization.
- Shared startup invokes timezone enforcement from both `sitecustomize.py` and settings: empty `PYTHONTZPATH`, reset zoneinfo search, exact `tzdata==2026.3`.
- Ruff selects `ALL`; `src = ["tests"]` controls import classification, not lint scope. `ops/` is included. Mypy is strict; pytest rejects unknown config/markers and treats warnings as errors.
- CI covers Python 3.12/3.13. `make ci` includes database posture, 90% covered-domain coverage, dependency auditing, image/TLS contracts, and browser contracts.

## ANTI-PATTERNS (THIS PROJECT)
- Never put real identifiers, credentials, session/OTP material, clinical content, or production telemetry into fixtures, logs, screenshots, evidence, or commits.
- Do not describe deferred domains as shipped: eight service seams raise `NotImplementedError("Phase >=1")`; Redis/Celery/DRF dependencies do not establish working jobs or domain APIs.
- Never grant runtime ownership/bypass access to make tests pass; security-sensitive AI-assisted changes require the human review and targeted checks in `docs/CONTRIBUTING.md`.
- Do not change frozen approved-plan bytes/checksum under `docs/plans/` to write a new roadmap; CI authenticates these inputs.
- No GitHub Actions `services:` containers: workflows use the claimed database lifecycle. Workflow pin changes also require the provenance allowlist and `ops/testing/action-provenance.json`.
- Do not run Terraform plan/apply for foundation validation or commit credentials, backend values, state, or saved plans. The provider lockfile is intentional.
- Do not treat synthetic restore rehearsal or unapproved compliance reviews as live-data readiness.

## UNIQUE STYLES
- `templates/base.html` directly loads four first-party CSS files, vendored HTMX, and `static/js/auth-ui.js`; this asset path has no frontend bundler.
- HTMX form busy state/error focus uses `data-auth-form` and `data-focus-error`; fragments live beside their domain screens under `partials/`.
- `DESIGN.md` defines warm paper surfaces, restrained teal actions, rust focus accents, tonal separation without decorative shadows, and a 4px spacing base. New colors require updating its palette first.

## COMMANDS
Commands below are documented entry points, not verification results. Configure the environment and claimed DB resources through `docs/RUNBOOK.md` first.
```bash
uv sync --locked --all-groups
make isolated-db-up       # Requires claim/project/volume inputs and a non-5432 loopback host port
make isolated-db-status
make db-bootstrap && make migrate && make db-posture
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run pytest --reuse-db tests
uv run ruff check . && uv run ruff format --check .
uv run mypy .
make ci                   # Full quality, DB, dependency, image/TLS, browser pipeline
make ci-image-contracts
make ci-browser-contract
python ops/testing/validate_action_pins.py
make isolated-db-down
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
```

## NOTES
- WSGI and ASGI entry points default to `config.settings.prod`; tenant middleware remains synchronous. `config.urls_dev` adds debug-only identity showcase routes.
- Synthetic-only enforcement occurs at settings import, not just in documentation. Scheduling is staff-facing, not patient self-booking/waitlists/reminders.
- `tests/browser/test_runtime_https.py` uses fake pages/contexts; it is not live TLS or Playwright verification. Live runner contracts belong to the scoped ops guides.
- `.omo/` plans/drafts are local workflow artifacts and are gitignored; existing evidence and runner inputs retain shared-worktree responsibilities recorded under `docs/maintenance/`, if present locally.
- Historical `initial_plan_en.md` and `initialreport.md`, if present locally, are not current regulatory, vendor, pricing, or delivery authority.
