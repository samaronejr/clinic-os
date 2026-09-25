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
test database creation and migration remain owner responsibilities.

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
uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner ci
```

`browser --suite <name>` runs one registered suite (the full registered
set is enumerated by `SUITES` in `ops/testing/renewal_runner.py`). In hosted
CI the `renewal-browser` matrix shards every registered suite across six
jobs and the `Renewal RC acceptance` verdict binds shard list, per-suite
reports and source digests. It captures the working tree
through the current-source snapshot contract, provisions a unique
`postgres:16` container and volume, applies migrations and seeds a clinic as
`clinic_owner`, serves through a supervised Gunicorn master on loopback as
`clinic_app` with the real middleware/CSRF/RLS stack, waits on `/readyz`,
then drives the suite through real Chromium. The junit verdict is enforced:
zero tests, skips, failures, errors or a missing report all fail the run.

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

`.github/workflows/ci.yml` adds, alongside the frozen three-suite
`contracts` selector and the `test` matrix: `renewal-browser` (all
registered suites sharded across six jobs), `worker-integration` (real
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
label values come from closed sets (URL route names, Celery queue names,
registered capability keys, status classes), and open fields validate to
strict machine formats or collapse to `[invalid]`. Sentry events are rebuilt
recursively — `contexts.trace`, breadcrumbs, request, tags and exception
keep only validated structural fields. Queue age requires the enqueue-time
header stamped by `before_task_publish` in `CoreConfig.ready`; unstamped
legacy messages simply omit `clinic_queue_oldest_age_seconds`. Gunicorn
access logs (`ops/container/gunicorn_no_proxy.py` `access_log_format`, also
passed as `--access-logformat` to the browser harnesses) emit only method,
status and duration — never the request line, query or peer address.

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

## Disposable logical recovery rehearsal

`make restore-rehearsal` is called only after the F3 supervisor has stopped
source writers, migrated a distinct task-owned target as `clinic_owner`, and
passed the two container IDs/database names plus their lease claim IDs, one
private credential FD (source/target superuser passwords plus the target
`clinic_app` password and TLS URL) and task-owned mode-0700 work/evidence
paths. Before any mutation the command authenticates that each container
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
`tenant_decrypt` under the configured KEK, and every attachment object byte
against the restored object store. It deletes and proves absence of the dump
and hash. Restored verification is read-only except for advancing each
restored sequence once; it runs no restored provisioning or browser command.

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
