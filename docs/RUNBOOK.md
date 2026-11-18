# Clinic OS Phase 1A synthetic runbook

This runbook covers the source-backed Phase 1A synthetic surface. It does not
authorize a production deployment. Security boundaries are in
[SECURITY.md](SECURITY.md), and contribution gates are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Prerequisites

- Docker Engine with the Compose plugin
- `uv` and a supported Python version (3.12 or 3.13)
- `curl`
- a real Chrome or Chromium executable for the renewal browser route
- optional validation tools: `actionlint` and Terraform

The compose service is PostgreSQL 16. The repository's default local port is
5432; stop conflicting local services or use an isolated Compose override in
automation. Never reuse the example credentials outside a disposable local
environment.

## First local start

Create an ignored local environment file and replace every placeholder with a
unique development value:

```sh
cp .env.example .env
set -a
. ./.env
set +a
```

Keep each `CLINIC_*_PASSWORD` synchronized with the matching password component
in `APP_DATABASE_URL`, `MIGRATION_DATABASE_URL`, and
`TEST_SUPERUSER_DATABASE_URL`. Use shell-safe local values when sourcing the
file. Compose reads `.env` itself, but Make and direct `uv` commands need the
variables exported into the current shell as shown above.

Start PostgreSQL, wait for its health check, bootstrap the fixed roles and the
pre-created test database, apply migrations as `clinic_owner`, and verify the
runtime posture:

```sh
docker compose up -d --wait db
make db-bootstrap
make migrate
make db-posture
```

`db-bootstrap` is idempotent and applies the same role/database posture to the
development and pre-created test databases. `make migrate` intentionally
substitutes `MIGRATION_DATABASE_URL` for the Django connection; never migrate
with the `clinic_app` runtime role.

Start the web process as the runtime role:

```sh
APP_DATABASE_URL="$APP_DATABASE_URL" uv run python manage.py runserver 127.0.0.1:8000
```

From a second shell:

```sh
curl --fail --silent --show-error http://127.0.0.1:8000/healthz
curl --fail --silent --show-error http://127.0.0.1:8000/readyz
```

Both return `{"status": "ok"}` when the process and database are ready.
`/healthz` never queries the database; `/readyz` runs `SELECT 1` and returns
HTTP 503 with `{"status": "unavailable"}` when the database probe fails.

## Tests and quality gates

The repository gate is:

```sh
make ci
```

It performs a locked dependency sync, idempotent database bootstrap, owner
migration, runtime posture check, Ruff lint and formatting checks, strict mypy,
the complete test/coverage suite, and a local dependency audit. Tests use the
pre-created database and explicitly exercise `clinic_app` runtime connections;
test database creation and migration remain owner responsibilities. Local test
runs never connect to the workstation's Redis: `config.settings.test` defaults
`CELERY_BROKER_URL` to the in-memory `memory://` transport unless the variable
is exported explicitly (the hosted `worker-integration` gate sets its own
isolated Redis URL).

For a focused recent-verification check after bootstrap/migration:

```sh
uv run pytest --reuse-db tests/auth/test_stepup_policy.py tests/auth/test_stepup_challenge.py tests/auth/test_stepup_sessions.py tests/auth/test_stepup_telemetry.py tests/auth/test_stepup_ui.py
```

Optional repository validation, without provisioning infrastructure:

```sh
actionlint .github/workflows/ci.yml
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
```

Never run Terraform plan/apply as part of this foundation runbook.

## Renewal verification runner

`ops/testing/renewal_runner.py` is the verified current-source route for
uncommitted work. `make ci` still owns the committed-source gate; its image
contract builds the committed revision, so it requires a clean Git tree.

```sh
uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner browser --suite smoke
CLINIC_BROWSER_ENGINE=webkit uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner browser --suite smoke
uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner browser --suite primitives --theme dark --density compact
uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner ci
```

`browser --suite <name>` runs one registered suite (the full registered
set is enumerated by `SUITES` in `ops/testing/renewal_runner.py`). In hosted
CI the `renewal-browser` matrix shards every registered suite across six
Chromium jobs, and runs every registered suite again on Firefox and on WebKit
in three `suite@engine` shards per engine. The `Renewal RC acceptance`
verdict binds the shard list, per-leg reports and source digests. It captures the working tree
through the current-source snapshot contract, provisions a unique
`postgres:16` container and volume, applies migrations and seeds a clinic as
`clinic_owner`, serves through a supervised Gunicorn master on loopback as
`clinic_app` with the real middleware/CSRF/RLS stack, waits on `/readyz`,
then drives the suite through a real browser engine. Chromium is the default
(CI parity); `--engine firefox|webkit` or `CLINIC_BROWSER_ENGINE` selects the
Playwright-managed Firefox or WebKit (`uv run playwright install firefox
webkit`; add `--with-deps` or the listed system packages on a new host), and
`report.json` records the `engine`. An unavailable engine fails the run; it
never skips or falls back. Every suite runs on every engine. The
engine-specific mechanisms (fake or synthetic camera/microphone, the 200%
zoom, clipboard read-back, session history) and each documented engine
difference live in `tests/renewal/browser/engines.py`. `ci` always runs
Chromium. Runner children use the in-memory Celery broker (`memory://`), so
no run reaches a host Redis. The junit verdict is enforced: zero tests, skips,
failures, errors or a missing report all fail the run.

`ci` runs the gates in order and aggregates per-command exits into
`ci-report.json`: static (Ruff check, Ruff format, strict mypy), migration
drift, full coverage against a provisioned database, `pip-audit --local`,
the current-source image build plus TLS smoke, and every registered browser
suite.

Prerequisites beyond the base list:

- Docker for the provisioned database and image builds.
- A Chrome/Chromium executable on `PATH`, or an absolute non-symlink path in
  `CLINIC_RENEWAL_BROWSER_EXECUTABLE`.
- `CLINIC_RENEWAL_BUILD_HOST_NETWORK=1` on this workstation: the VPN's
  1412-byte MTU blackholes Docker bridge egress, so image builds use
  `--network=host`. Image bytes are identical to the stock builder.
- PDF inspection tests use Poppler (`pdftotext`, `pdftoppm`). Hosted CI
  installs `poppler-utils` and exports `CLINIC_PDF_TOOLS=required`, which
  turns a missing binary into a hard failure; without that variable a
  local run may skip those artifact checks as a convenience.
- The image/TLS gate (`smoke-current-source`) reserves claims through the
  repository ledger `.omo/evidence/isolation-ledger-phase1a.json`, which must
  be open and same-boot. The retained ledger in this worktree is a prior-boot
  evidence record, so the gate rejects it here; run that gate from a checkout
  whose ledger was provisioned in the current boot (the tracked-CI snapshot
  command in `.github/workflows/ci.yml` is the reference). Never overwrite
  the retained ledger to satisfy the gate.

The runner exports `CLINIC_RENEWAL_BASE_URL`, `CLINIC_RENEWAL_ARTIFACT_ROOT`,
`CLINIC_RENEWAL_BROWSER_EXECUTABLE`, `CLINIC_RENEWAL_USERNAME` and
`CLINIC_RENEWAL_PASSWORD` privately to its own suite processes; they are
never written to the repository. `CLINIC_RENEWAL_APP_DATABASE_URL` may point
serving at an existing database, but the DSN must use the `clinic_app` role
on loopback with a non-5432 port; owner or superuser DSNs are rejected.
Cleanup removes only resources the runner created; the artifact root is
retained as evidence.

### Hosted RC gates

The SHA-256-pinned `ops/testing/ci-required-browser-suites.txt` also requires
`clinical-inbox`. Local `make ci-browser-contract` sends the three legacy
image suites to `browser_runner.sh` and the inbox to `renewal_runner`.
The final image-runner selector remains a separate input.

`.github/workflows/ci.yml` adds, alongside the frozen three-suite
`contracts` selector and the `test` matrix: `renewal-browser` (all
registered suites sharded across six Chromium jobs, plus Firefox and WebKit
legs declared as `suite@engine` shard entries), `worker-integration` (real
isolated `redis-server` plus a separate Celery worker proving
rollback/lost-dispatch/idempotency/revocation/retry-ceiling/callback
behaviour; `CLINIC_BROKER_GATE=required`), `migration-upgrade`
(fresh install plus `merge-base`→candidate rehearsal preserving seeded
patient/scheduling/role/audit rows), and `renewal-acceptance` — an
`if: always()` aggregate that rejects failed, cancelled, missing or
divergent-digest evidence. Poppler is provisioned in the `test` job and
gated by `CLINIC_PDF_TOOLS=required`.

### Runner stops and recovery

Every rejection exits nonzero with a named cause. The four preflight checks
(missing browser, unknown suite, invalid serving DSN, and stale source) run
before provisioning, and no fake browser or partial result is substituted.
Suite assertion, zero-test, and interrupt failures can occur after
PostgreSQL/Gunicorn are running; the runner tears down its owned
container/volume/processes and retains the diagnostics generated under the
artifact root (JUnit, pytest, server, and access logs, plus browser captures).
`report.json` is written only after a successful run. Fix the named cause and
re-run the same command.

| Symptom | Exit | Cause and recovery |
| --- | --- | --- |
| `renewal browser executable is unavailable` | 2 | No Chrome/Chromium on `PATH`; install one or set `CLINIC_RENEWAL_BROWSER_EXECUTABLE` |
| `renewal browser executable override is not executable` | 2 | The override path is not an absolute, non-symlink, executable file |
| `renewal browser suite is not registered: <name>` | 2 | Unknown suite; check `SUITES` in `ops/testing/renewal_runner.py` |
| `renewal browser engine is not supported: <name>` / `name different engines` | 2 | Use `chromium`, `firefox` or `webkit`, and do not pass an `--engine` that contradicts `CLINIC_BROWSER_ENGINE` |
| `renewal browser engine <name> is unavailable` | 2 | Run `uv run playwright install <name>` (plus `--with-deps` or its system packages) |
| `renewal serving DSN must use the clinic_app role` | 2 | `CLINIC_RENEWAL_APP_DATABASE_URL` names an owner/superuser role; use the app role or unset it |
| `current-source record is stale or drifted` | 2 | A `--record` no longer matches the working tree; re-capture the record |
| `renewal suite failed` / `ran zero tests` / `skipped tests` | 2 | The suite itself failed or proved nothing; inspect `pytest.log` and the junit XML under the artifact root |
| `smoke-current-source` fails inside `ci` | 1 | Usually the prior-boot ledger gate above; the `ci` report records the real exit rather than weakening the check |
| `renewal runner interrupted` | 130 | SIGINT/SIGTERM; owned containers, volumes and processes are removed before exit |

## Celery queues and workers

Celery workloads are split across seven queues so one tenant's bulk or AI
burst cannot delay another tenant's clinical work. Queue names are declared
in `config/celery.py` and tasks route by module prefix; the existing
`comms.*` outbox tasks stay on `clinic-integrations`.

| Queue | Workload |
| --- | --- |
| `clinic-integrations` | comms outbox operations and reminder dispatch (existing) |
| `clinical` | chart-facing tasks; isolated and fail-open under a Redis outage |
| `ai-interactive` | latency-sensitive AI work (for example scribe chunk processing) |
| `ai-batch` | deferred AI work (drafting, extraction); per-tenant quota |
| `messaging` | patient messaging tasks; per-tenant quota |
| `finance` | billing, insurance and subscription tasks; per-tenant quota |
| `bulk` | imports, retention and workflow runs; per-tenant quota |

Run one worker per queue (or a small set of queues) with the broker URL
exported in the environment:

```sh
uv run celery -A config.celery worker -Q clinic-integrations -c 2
uv run celery -A config.celery worker -Q clinical -c 2
uv run celery -A config.celery worker -Q ai-interactive -c 2
uv run celery -A config.celery worker -Q ai-batch -c 2
uv run celery -A config.celery worker -Q messaging -c 2
uv run celery -A config.celery worker -Q finance -c 2
uv run celery -A config.celery worker -Q bulk -c 2
```

Beat is unchanged and schedules only the existing `comms.*` tasks:

```sh
uv run celery -A config.celery beat
```

Per-tenant fairness is enforced by `apps.core.fairness.fair_acquire`, a
Redis token bucket keyed on `(organization, queue)`. Quotas are tasks per
minute, published per organization through the validated
`ClinicConfiguration.queue_quotas` map. Quota edits require
organization-wide authority — an owner or clinic-admin role on every
clinic of the organization until the planned org_admin role exists — so
no single clinic's admin can move the organization's limits, and an
ordinary clinic settings save carries the organization's effective map
forward unchanged. A task on a regulated queue calls `acquire_or_defer`
first; when the bucket is empty the task is re-enqueued with a 30-second
countdown and a `clinic_fairness.deferred` metric is emitted. If Redis is
unreachable, `bulk`, `ai-batch` and every other queue fail closed (defer)
while `clinical` fails open so chart saves are never blocked by metering.
A malformed stored quota fails closed as well; it never falls back to a
larger allowance.

## Degraded-mode drills

Every synthetic provider adapter honours one fault toggle, so a drill can make
one provider family fail while the rest of the product keeps working. Set
`CLINIC_SYNTHETIC_FAULTS` to comma-separated `family=mode` pairs, for example
`CLINIC_SYNTHETIC_FAULTS=ai=slow,payment=down`. The families are `ai`, `video`,
`messaging`, `signing` and `payment`; the modes are `down`, `slow` and
`ambiguous`. An unknown family or mode, a repeated family, or any fault in live
data mode stops the process at startup, and live activation lists the variable
as synthetic-only. Each adapter answers with its own existing failure
vocabulary, so the drill exercises the real failure handling:

| Family | `down` | `slow` | `ambiguous` |
| --- | --- | --- | --- |
| `ai` (gateway, scribe ASR) | `provider_unavailable`; nothing retained | `provider_timeout`; the circuit opens and the settings page shows degraded | over-limit truncated reply, rejected as `invalid_output` |
| `video` | room operation retried, then failed | same as `down` | held `in_progress` for reconciliation, never re-sent |
| `messaging` (reminders, inbox) | message retried later (`transient_send`) | same as `down` | held `in_progress` for reconciliation, never re-sent |
| `signing` | operation stays `prepared` for retry | same as `down` | acceptance without a usable reference fails closed (`invalid_provider_reference`) |
| `payment` | PIX code not generated; PSP status `unknown`, lookup-only recovery | same; a PSP create that may have happened stays `unknown` | PSP status `unknown`, lookup-only recovery |

No fault reroutes work to another provider, marks a payment paid or issues a
signature. Scheduling, charting and ordinary billing (invoice and manually
confirmed settlement) need no provider and keep working with every family
faulted. Portal messages stay inside the system and are never faulted. The
drills are `tests/resilience/` (pytest on real roles, including real worker
processes killed with SIGKILL right after a provider effect) and the
`resilience` browser suite. Unknown AI charges stay conservatively accounted;
a `provider_unavailable` failure does not open the circuit, so only the
failed request shows unavailable.

A worker killed after the provider effect never repeats it: an abandoned
message reconciles without a resend, a PSP charge recovers by lookup only, and
a signing operation is asked again under the same operation id, which a real
signing adapter must honour as its idempotency key. The orphaned provider
operation can never issue: its callback is rejected before scope lookup.

## Realtime transport

Realtime carries refetch hints, never records, and is off unless
`REALTIME_ENABLED` is true; the agenda keeps working without it and falls back
to 30-second polling when `REALTIME_POLLING_FALLBACK` is true. Ordinary HTTP,
including the ticket POST to `/rt/stream`, stays on Gunicorn. Only
`GET /rt/stream?t=<ticket>` goes to the ASGI process
(`config.asgi_realtime:application`, the image's `--target realtime` uvicorn
command). Tickets live 60 seconds in Redis (`REALTIME_REDIS_URL`) and are
consumed once. A stream closes after at most 600 seconds and on revocation.

- Redis down: streams close and new tickets are refused. Booking and charting
  are unaffected and the agenda polls. Do not point realtime at the Celery
  broker database to "fix" it.
- ASGI process down: restart it. No state is lost; clients refetch on the
  next poll.
- Hosted routing must send only `GET /rt/stream` to ASGI, disable proxy
  buffering, keep a read timeout above 600 seconds and never log the stream
  query string, which carries the ticket.

Details and the verification commands are in
[apps/realtime/README.md](../apps/realtime/README.md).

## AI kill switches

AI is off by default. Three switches stop it, from widest to narrowest, and
each is rechecked at admission and again at the final send boundary:

1. Global (owner CLI, as `clinic_owner`):
   `uv run python manage.py ai_gateway global-switch --disable`. Re-enable
   only with an approved region:
   `uv run python manage.py ai_gateway global-switch --enable --approved-region br-synthetic`.
2. Organization opt-in and capability enablement: the TOTP-protected AI
   settings page at `/clinics/<clinic_id>/settings/ai/`.
3. Circuits: a provider timeout opens one organization/configuration circuit.
   Close it after the cause is fixed with
   `uv run python manage.py ai_gateway circuit-reset --organization-id <uuid> --clinic-id <uuid> --configuration-id <uuid>`.

Turning AI off never blocks clinical work: notes, scheduling and messages
continue through their manual paths. An AI request that was already admitted
fails closed with its own reason code and is not retried on another provider.
See [apps/ai/README.md](../apps/ai/README.md).

## Provider degrade and revoke

Provider availability is read from the capability lifecycle registry
(`apps/providers`), never from a constant. Owner commands, run as
`clinic_owner`, record each change with an approver, a role and an evidence
link, and append a `providers.capability.*` audit event:

```sh
uv run python manage.py provider_capability degrade --key <key> --approver-name <name> --approver-role <role> --evidence-uri <uri> --reason <text>
uv run python manage.py provider_capability revoke --key <key> --approver-name <name> --approver-role <role> --evidence-uri <uri> --reason <text>
uv run python manage.py provider_capability report --markdown
```

`degrade` moves an activated capability to `degraded`; `activate` returns it.
`revoke` is terminal: a new capability version is needed to use that provider
again. Neither command reroutes work to a different provider. The reason and
evidence text go to owner records only, so keep customer and patient data out
of them. Support diagnostics show each capability's state to a support user
under a customer grant.

## Support diagnostics

Support never borrows a staff context. A clinic administrator with
organization-wide authority approves a `SupportAccessGrant` on the enterprise
governance screen for one named, active support account, with a purpose and at
most four hours. The approval can be revoked there at any time and stops
working at the next request.

The support user signs in with their own verified account and opens
`/support/diagnostics/`. The clinic ID and grant ID travel only in the POST
body. The screen re-verifies the grant and the browser session in PostgreSQL
(`enterprise_support_scope`, then `enterprise_support_overview`) and shows:

- integration status: each provider capability and its lifecycle state;
- the integration queue: outbox operations by channel and status, with the age
  of the oldest waiting one;
- task health: work tasks by state and whether any open task is overdue;
- a masked tenant overview: the number of units and active staff by role.

It shows no patient, name, username, identifier, message, error text, payload
or clinical record, and has no write action. A missing, expired, revoked or
foreign grant and an unverified session all get the same refusal. Every read
appends `identity.support.read` against the named support user and the grant.
The enterprise policy fence keeps every other tenant table empty under a support
grant, so a support user who opens a patient chart or a staff page is refused.

No support desk is staffed in this version. The procedure below is rehearsed
on synthetic data only, and no customer support commitment or service level
exists yet.

### Support drills

Each drill is an agent-executed synthetic simulation in
`tests/resilience/test_support_drills.py`. In each one the customer approves
access, the support user reads the screen, and the fix stays with the customer
or the owner:

| Report | What support sees | Who fixes it |
| --- | --- | --- |
| Lost authenticator | no account, device or code data; support's own account cannot request an authenticator reset for the customer's clinic | the user, with one saved recovery code, or a clinic manager through the delayed, notified authenticator reset |
| Failed payment callback | provider capability states and counts, never the charge, amount or provider reference; a forged callback is rejected before scope lookup | the clinic's finance user, through payment reconciliation |
| AI outage | one failed action in the integration queue and the model capability state, never the AI input or output | the owner, with the AI kill switches and circuit reset above; clinical work continues manually |

Run them with
`uv run --frozen --no-sync --no-env-file pytest --reuse-db -q tests/resilience/test_support_drills.py`.
Each test records a `support_drill` JSON property in its JUnit report.

## Status page incidents

Incidents are posted by hand in `docs/operations/status-incidents.json` and
shown at `/status/`. Automated monitoring alerts are not connected (monitoring
delivery is still pending in LIVE-DATA-GATE.md), so the page can lag a real
problem. An incident has an id (`INC-<year>-<number>`), one or more components
(`agenda`, `records`, `portal`, `messaging`, `payments`, `video`, `ai`), an
impact (`maintenance`, `degraded`, `partial_outage`, `major_outage`) and dated
updates whose states are `investigating`, `identified`, `monitoring` and
`resolved`. The page builds every sentence from those keys, so there is no
free-text field: never try to add customer, patient or error details. The
file may be at most 256 KiB (`MAX_STATUS_BYTES` in `apps/website/status.py`)
and no object may repeat a key. A file that breaks the model, exceeds the limit
or repeats a key makes the page say status is unavailable, never an all-clear. Validate a change with
`uv run --frozen --no-sync --no-env-file pytest -q tests/website/test_status.py`
before deploying it.

## Stop and reseed

Stop containers while preserving local database data:

```sh
docker compose down
```

To rebuild a disposable local database from zero:

```sh
docker compose down -v
docker compose up -d --wait db
make db-bootstrap
make migrate
make db-posture
```

`docker compose down -v` permanently deletes the Compose database volume. Use
it only for disposable local data after confirming no needed work exists in
that volume. It is not a production recovery procedure.

## Internal metrics and tracing

`GET /internal/metrics` serves the Prometheus text exposition (request
latency by route name, queue depth/age, outbox states, provider health by
capability, AI invocation aggregates). It is sessionless and fail-closed:
requests must come from `CLINIC_OPS_METRICS_ALLOWED_NETWORKS` (default
loopback only) and carry `Authorization: Bearer $CLINIC_OPS_METRICS_TOKEN`
(minimum 16 characters; unset or short token denies every request). The
endpoint never labels metrics with patient or clinic names, raw URLs or
request bodies (ADR-014).

Structured JSON logs and the Sentry scrubber share the allowlist in
`apps/core/telemetry.py`. The boundary is a closed vocabulary: log messages
must be registered in `LOG_MESSAGE_ALLOWLIST` (args are never interpolated),
and every emitted value is either a closed-set member or a validated id.
Route labels are registered URL names, namespaced names included
(`identity:login`); provider labels are `PROVIDER_CAPABILITY_KEYS` (the v2
integration record set, pinned by test to the `apps/providers` seed); AI labels
are `AI_INVOCATION_PURPOSES` (to be replaced by todo 38 model purposes).
Logger names must resolve to an imported module or a framework logger.
Sentry events are rebuilt from those vocabularies: the SDK timestamp is
re-rendered from a strict parse, and `server_name`, `extra`, `user`,
messages and nested `data` never leave. Options the SDK would otherwise infer
from the environment are passed explicitly, because inferred values reach
session and client-report envelopes outside `before_send`: `environment`
is `SENTRY_ENVIRONMENT` only when it is a listed value, `release` is
`SENTRY_RELEASE` only when it is a 40-hex SHA (else the fixed `unversioned`),
`server_name` is fixed, automatic session tracking and Spotlight are off,
transactions are dropped and check-ins are discarded. Set `SENTRY_RELEASE`
to the deployed commit SHA to get release attribution.

Celery workers use the same pipeline: `config/celery.py` connects
`setup_logging`, applies Django `LOGGING` and redirects task stdout into
it, so Celery never installs its own handlers (whose formats render task
args, kwargs and return values). Django's DEBUG console, `mail_admins` and
runserver handlers are likewise replaced. Queue age requires the
enqueue-time header stamped by `before_task_publish` in `CoreConfig.ready`;
unstamped legacy messages simply omit `clinic_queue_oldest_age_seconds`.
Gunicorn access logs (`ops/container/gunicorn_no_proxy.py`
`access_log_format`, also passed as `--access-logformat` to the browser
harnesses) emit only method, status and duration, never the request line,
query or peer address.

OpenTelemetry tracing is disabled by default: set `CLINIC_OTEL_ENABLED=1`
plus `CLINIC_OTEL_EXPORTER=console` or `otlp-http-json` with
`CLINIC_OTEL_OTLP_ENDPOINT=<collector base URL>`. Span names resolve to
route names, status descriptions are dropped (they carry exception
messages), and exporter failures are contained and never affect requests.

## Troubleshooting

### Bootstrap or posture fails

1. Run `docker compose ps` and confirm the `db` service is healthy.
2. Confirm the shell exported all variables from the ignored `.env` file.
3. Confirm `POSTGRES_CONTAINER` is either unset (auto-detected by Make) or the
   exact container ID/name, and `POSTGRES_PORT` is the PostgreSQL port inside
   the container.
4. Re-run `make db-bootstrap`; it is designed to be idempotent.
5. Run `make db-posture`. Its expected summary identifies `clinic_app` as the
   runtime role, `clinic_owner` as `NOCREATEDB`, and the test database as
   pre-created.

Do not fix posture failures by granting owner, superuser, BYPASSRLS, schema
ownership, or direct `identity_user` access to `clinic_app`.

### Migrations or schema checks fail

- Verify `MIGRATION_DATABASE_URL` names `clinic_owner` and the same database as
  the runtime URL, then run `make migrate`.
- Check for uncommitted model changes with
  `uv run python manage.py makemigrations --check --dry-run`.
- Run `uv run pytest --reuse-db tests/infra/test_schema_policy.py tests/identity/test_tenancy_rls.py`.
  The tests require the exact tenant-table set, FORCE RLS, and one fail-closed
  policy per table. A newly introduced tenant model must be added to the
  migration/RLS matrix; weakening the test is not remediation.
- Use `uv run python manage.py showmigrations` for diagnosis. Do not edit an
  already-shipped migration to hide drift.

### Authentication or tenant access returns 403

- Confirm the user is active and has a `UserClinicRole` for the selected
  organization and clinic.
- Confirm the signed session contains the expected `active_org_id`. The
  middleware intentionally rejects missing, malformed, or non-member choices.
- Do not query `identity_user` with `clinic_app`. Authentication must pass
  through `auth_lookup`, and request rehydration through `load_current_user`.
- Re-run the identity, middleware, and resolver tests before changing role or
  function grants.

### TOTP or step-up loops

- Confirm the user has a confirmed TOTP device owned by that exact user.
- Password login intentionally clears prior device/freshness state. Complete
  ordinary TOTP verification again.
- Recent verification expires after the configured window (300 seconds by
  default) and rejects future or malformed timestamps. Re-enter a current OTP
  at `/auth/step-up/`; do not modify session storage manually.
- Unsafe or authentication-loop `next` targets are intentionally replaced by
  the safe default. A user with no confirmed device receives a fail-closed
  response instead of an enrollment bypass.

### Audit verification fails

Stop mutation-capable application work, preserve the payload-free verification
evidence, and follow the private incident process in
[SECURITY.md](SECURITY.md). Do not repair, delete, truncate, or resequence audit
rows. Compare database roles, functions, triggers, and migration state against
the repository before deciding on recovery.

## Secret rotation and incidents

For a suspected credential exposure, revoke or rotate the affected secret in
the authoritative deployment system, restart dependent processes, verify
database role posture and authentication, and invalidate affected sessions.
Do not paste old/new credentials, OTP seeds, database URLs, patient identifiers,
or clinical data into issues, logs, evidence files, or chat. Local example
credentials are disposable only; production rotation requires the provider's
approved procedure and a recorded incident timeline.

### Tenant key rotation

Tenant data keys (DEKs) live only wrapped in `tenancy_tenantdatakey`; one tenant
KEK in the configured secret store unwraps them. Both rotations run as
`clinic_owner` with the tenant GUC set, through `apps.tenancy.envelope`:

1. DEK rotation: `issue_tenant_key()` creates a new active version and retires
   the old one. Retired versions keep opening existing ciphertext and their
   blind indexes stay searchable; `reencrypt` moves one envelope forward.
2. KEK rotation: with the old KEK still configured, call
   `rewrap_tenant_keys(new_kek=...)` for every organization. Switch the
   configured KEK only after every organization is rewrapped: a tenant that
   was not rewrapped cannot be read under the new KEK. A rewrap under the wrong
   KEK changes nothing; it aborts as a whole.
3. Escrow the new KEK immediately, before the next backup. An escrow that still
   holds the old KEK opens nothing, and the recovery exercise refuses it.

`tests/resilience/test_key_rotation.py` drills this order on real roles,
including the stranded tenant of a partial rotation. Production key
management, KMS policy and escrow custody remain deployment decisions behind
EG-12; this is a synthetic procedure.

## Incident response

The private incident process is in [SECURITY.md](SECURITY.md). This section
maps who does what and the timing duties, so the on-call team does not have to
work them out during an incident. Owners, counsel and contacts are still
unassigned and [INCIDENT-APPROVAL.md](compliance/INCIDENT-APPROVAL.md) is
unapproved, so this is a draft for EG-2 and EG-12 review, not legal advice.

Roles:

- Incident commander (Clinic Ops on-call engineer): owns triage, containment,
  the timeline and the restricted incident record.
- Communications lead (Clinic Ops): drafts the customer, ANPD and data-subject
  texts for the controller; never sends patient data through tickets or chat.
- Clinic controller and its encarregado (DPO): the clinic decides and makes
  the ANPD and data-subject communications. Clinic Ops is the operator and
  supports them.
- Recovery owner: runs containment and recovery steps on the provider's
  approved procedure; the synthetic recovery exercise below is only the
  technical rehearsal of that procedure.

Timeline:

1. Detection (T0): page the incident commander; preserve payload-free evidence
   such as audit-chain verification output and role/policy posture.
2. Containment: revoke or rotate the affected credentials, use the AI kill
   switches to stop affected AI flows, and keep scheduling and charting
   available through their manual paths.
3. Assessment: decide whether personal data were affected and whether the
   incident may cause relevant risk or damage (CD/ANPD Res. 15/2024 arts. 4 and
   5). Health data are sensitive personal data, one of the art. 5 criteria.
4. Controller notice: tell each affected clinic as soon as personal data are
   confirmed affected. The legal clock starts when the controller knows the
   incident affected personal data (art. 6, sec. 1), so the operator must not
   consume it; the contractual operator deadline belongs to the DPA (EG-5).
5. Communication: within three working days of that knowledge the controller
   communicates the incident to the ANPD (art. 6) and to the affected data
   subjects (art. 9), unless specific legislation sets another deadline.
6. Record: keep the incident record for at least five years from its date,
   including incidents that were not communicated (art. 10).
7. Recovery and closure: restore from the earliest trusted point, verify
   audit chains, rotate keys if they may be exposed, and record the lessons and
   follow-up tests.

These timing facts were re-read from the DOU text on 2026-11-11
([reverification-log.md](compliance/reverification-log.md), RV-06).

## Disposable logical recovery rehearsal

`make restore-rehearsal` is called only after the F3 supervisor has stopped
source writers, migrated a distinct task-owned target as `clinic_owner`, and
passed the two container IDs/database names plus their lease claim IDs, one
private credential FD (source/target superuser passwords plus the target
`clinic_app` password and TLS URL), the KEK escrow and object backup, and
task-owned mode-0700 work/evidence paths. Before any mutation the command authenticates that each container
carries the `clinic.phase1a.claim` label equal to the passed claim, lives
under the deterministic lease name derived from the claim token, and mounts
the claim-labeled pgdata volume; it then requires the target to hold zero
domain rows. The target migration leaf set must equal the source before the
fixed custom archive can mutate it.

The command uses only PostgreSQL 16.14 `pg_dump`/`pg_restore` clients inside the
exact source/target containers as isolated `clinic_super`. It writes a mode-0600
custom archive and SHA-256 sidecar, requires the normalized `TABLE DATA` and
`SEQUENCE SET` TOC to equal the fixed manifest covering every domain relation
(including attachments metadata, signing/payment references and the wrapped
`tenancy_tenantdatakey` store), restores strict data-only with no ownership or
ACL changes and triggers disabled for load order, then verifies row
fingerprints, owner/RLS/role-attribute/closed-table-ACL posture, the audit
hash chain plus recomputed canonical content hashes for every restored event,
restored DEK metadata, one source envelope decrypted on the target through
`tenant_decrypt` under the KEK restored from escrow, and every attachment
object byte against the restored object store. It deletes and proves absence
of the dump and hash. Restored verification is read-only except for advancing
each restored sequence once; it runs no restored provisioning or browser
command.

Rows, objects and keys are restored together (ADR-016). The command reads the
tenant KEK only from `RESTORE_KEY_ESCROW`, a private directory holding
`tenant-kek.secret`, never from the source's live secret store, and copies the
attachment objects only from `RESTORE_OBJECT_BACKUP` into the empty restored
object store. Before the target is touched it requires the escrow to exist and
to open the live source keys, and every attachment row to have its backup
object. Each gap fails with one explicit message: `restore drill failed: the
tenant KEK escrow is missing`, `... the escrowed tenant KEK cannot open the
source keys`, or `... N attachment object(s) are missing from the object
backup`. The restored workflows run against a secret store built from the
escrow and removed after verification. Purge tombstones are re-applied as
before. After verification the source is fingerprinted again: any write after
the recovery point fails the drill, so no acknowledged write is lost.

The evidence (schema 2) records the measured objectives against the plan's
proposals (RPO at most 300 s, RTO at most 3600 s including objects and keys).
The RPO is the data-loss window from the archive snapshot to the restore start;
the RTO runs from the restore start to the last proof. They are synthetic L1
measurements, not service commitments.

`python -m ops.testing.restore_rehearsal_acceptance` runs the whole exercise
in a host quiet window: it claims two TLS databases through the isolation
ledger, seeds the synthetic source, rotates its DEK and then its KEK, escrows
the rotated KEK, backs up the objects, proves the three failure variants
(missing KEK escrow, stale pre-rotation escrow, missing object) refuse before
the target is touched, and then restores. It prints the exercise report
validated by `docs/operations/recovery-exercise.schema.json`. The latest
recorded exercise, `docs/operations/recovery-exercise.json`, is shown with its
limits on the enterprise governance screen, which says so when none is
recorded.

Every relation in `clinic_app` has exactly one recovery classification in
`ops/testing/restore_contract.py`: restored data (`DOMAIN_RELATIONS`, which
includes per-user display preferences), a restored sequence
(`SEQUENCE_TARGETS`), required-empty source relations (`REQUIRED_EMPTY`), or a
target-owned exclusion with a written reason (`TARGET_OWNED_RELATIONS`). The
provider capability registry is platform data seeded by the target's own
migrations, so it is excluded. Before any target mutation, the rehearsal
requires the source registry to equal the target's seed, ignoring only
surrogate keys and timestamps. Owner-recorded provider approvals, activation
records and health events must be empty on the source: the rehearsal refuses
such a source rather than drop those records. `tests/infra/test_recovery_manifest.py`
derives every table, partition, sequence and view from the migrated catalog
and fails on any relation left unclassified, so a migration that adds a
relation must classify it in the same change.

Post-restore application proof runs as `clinic_app`, never `clinic_super`:
SQL probes assert tenant-scoped reads, cross-tenant RLS denial and the
closed ledger/DEK-unwrap ACLs, and the `ops.testing.restore_fixture verify`
subprocess exercises the real workflows on the restored target — patient
registry search through `patient_registry_*`, clinical version and
attachment decryption through the protected envelope boundary, the patient
charge resolver under the restored patient session, and row-by-row audit
chain verification through `apps.audit.verification`.

This rehearsal does not define a live backup principal and does not claim archive encryption or provider recovery. It is not reusable as an incident or
production procedure. See [LIVE-DATA-GATE.md](compliance/LIVE-DATA-GATE.md).

The Terraform skeleton enables an RDS backup-retention/PITR hook, but
`terraform validate` and this logical rehearsal are not provider restore tests.
Do not invent AWS or Terraform apply commands during an incident.

Source anchors: [Makefile](../Makefile), [Compose service](../docker-compose.yml),
[health views](../apps/core/views.py), [database posture](../ops/db/posture.py),
and [Terraform boundary](../terraform/README.md).
