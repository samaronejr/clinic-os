# Contributing to the Clinic OS foundation

Changes must preserve tenant isolation, the single RBAC authority, trusted
audit context, and explicit Phase 1A synthetic boundaries. Read
[ARCHITECTURE.md](ARCHITECTURE.md) and [SECURITY.md](SECURITY.md) before changing
identity, tenancy, audit, or a deferred domain.

## Local setup

Use Python 3.12 or 3.13, `uv`, Docker Compose, and PostgreSQL 16. Follow the
source-backed start sequence in [RUNBOOK.md](RUNBOOK.md): create an ignored
`.env`, start the database, run `make db-bootstrap`, `make migrate`, and
`make db-posture`, then use the runtime connection for the application.

Do not add a dependency unless the task explicitly requires it and existing
code cannot satisfy the boundary. Update and review `uv.lock` whenever an
approved Python dependency changes.

## Commit protocol

Use Conventional Commits and keep each commit small, buildable, and focused on
one decision. In this workspace, every commit is also a concise Lore decision
record. Begin with a why-oriented intent line, optionally add a short rationale,
then keep applicable trailers contiguous:

```text
Constraint: external constraint that shaped the decision
Rejected: alternative | reason it was rejected
Confidence: low|medium|high
Scope-risk: narrow|moderate|broad
Directive: warning for future modifiers
Tested: exact fresh verification
Not-tested: known validation gaps
```

Never commit credentials, `.env`, Terraform state/plans, generated caches,
database volumes, screenshots with patient data, or test artifacts containing
tokens or identifiers.

## Required gates

Run the smallest targeted suite while developing, then the repository gate
before handoff:

```sh
make ci
git diff --check
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
```

`make ci` owns the locked sync, database bootstrap/migration/posture, Ruff,
format, strict mypy, full test/coverage, and dependency-audit gates. Its
image contract builds committed source, so it requires a clean Git tree.

For uncommitted work, the renewal runner is the verified current-source
route:

```sh
uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner ci
uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner browser --suite smoke
```

It snapshots the working tree, provisions its own PostgreSQL container,
serves through Gunicorn as `clinic_app`, and drives a real Chromium. The
runner exports `CLINIC_RENEWAL_BASE_URL`, `CLINIC_RENEWAL_ARTIFACT_ROOT`,
`CLINIC_RENEWAL_BROWSER_EXECUTABLE`, `CLINIC_RENEWAL_USERNAME` and
`CLINIC_RENEWAL_PASSWORD` privately to its own suite processes; browser
fixtures skip without them, and the junit gate rejects skips, so a bare
pytest run can never fake a green browser pass. Prerequisites and failure
behavior are in [RUNBOOK.md](RUNBOOK.md#renewal-verification-runner).

When the optional tools are installed, also run:

```sh
actionlint .github/workflows/ci.yml
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
```

Do not run Terraform plan/apply for foundation work. Hosted GitHub Actions are
an additional Python 3.12/3.13 and actionlint signal; local completion cannot
claim a hosted run unless a remote, authentication, and an actual green run are
available.

## High-risk boundaries and targeted tests

Changes to tenancy/RLS, audit, identity/RBAC, TOTP, or step-up require
line-by-line self-review and fresh targeted tests before the full gate.

Tenant isolation and schema drift:

```sh
uv run pytest --reuse-db tests/infra/test_schema_policy.py tests/identity/test_tenancy_rls.py tests/identity/test_tenant_guc_boundaries.py tests/identity/test_tenant_middleware.py tests/identity/test_identity_isolation.py
```

Audit append, immutability, and verification:

```sh
uv run pytest --reuse-db tests/audit/test_audit_append.py tests/audit/test_audit_ledger.py
```

RBAC and relational integrity:

```sh
uv run pytest --reuse-db tests/identity/test_identity_rbac.py tests/identity/test_identity_tenancy_models.py tests/auth/test_auth_backend.py tests/infra/test_resolver_catalog.py
```

TOTP enrollment, isolation, session, redirect, and telemetry behavior:

```sh
uv run pytest --reuse-db tests/auth/test_2fa.py tests/auth/test_2fa_enrollment.py tests/auth/test_2fa_security.py tests/auth/test_2fa_sessions.py tests/auth/test_2fa_redirect_security.py tests/auth/test_2fa_telemetry.py tests/auth/test_totp_device_isolation.py
```

Recent-verification policy, challenge, session, telemetry, and UI behavior:

```sh
uv run pytest --reuse-db tests/auth/test_stepup_policy.py tests/auth/test_stepup_challenge.py tests/auth/test_stepup_sessions.py tests/auth/test_stepup_telemetry.py tests/auth/test_stepup_ui.py
```

## Migrations, tenant tables, and RLS

- Create schema changes through migrations and execute them as
  `clinic_owner`; never make `clinic_app` an owner or bypass role.
- Every new tenant table must carry an organization key, receive exact
  `ENABLE` plus `FORCE ROW LEVEL SECURITY` DDL and the fail-closed
  `tenant_isolation` policy, and pass `tests/infra/test_schema_policy.py`. Update the
  expected table/policy matrix in the same reviewed change. Every new tenant
  table must pass schema drift before merge.
- Preserve the request order: set `app.current_user_id`, validate membership
  through `user_has_org`, then set transaction-local `app.current_tenant`.
- Resolver/auth functions stay fixed-SQL, `SECURITY DEFINER`, owned by the
  `NOLOGIN` `clinic_resolver`, pinned to a trusted `search_path`, revoked from
  `PUBLIC`, and exposed only through the exact needed execute grants.
- Preserve the composite membership foreign key. Do not create an alternate
  tenant lookup or authorization table.
- Do not weaken a test, add a permissive bypass policy, or grant direct
  `identity_user` access to make a change pass.

## Audit, RBAC, TOTP, and step-up rules

- `UserClinicRole` is the only application RBAC authority. Authorization must
  name the clinic and allowed stored roles and must remain tenant-scoped.
- Tenant audit events derive organization and actor from GUCs. Do not add
  caller-supplied organization/actor parameters or direct base-table writes.
- Audit payloads use only the existing fixed metadata vocabulary. Do not place
  patient, credential, token, free-form instruction, or clinical content in
  the ledger.
- Preserve version/domain separation, RFC 8785 canonicalization, per-tenant
  serialization, system-chain restrictions, immutability triggers, and
  payload-free verification failures.
- TOTP tokens and seeds are sensitive. Preserve exact-device ownership,
  confirmation, replay/throttle checks, secret filtering, safe redirects,
  private cache headers, and session rotation.
- Operation-specific step-up must call `assert_step_up` at the service boundary
  or use `require_recent_verification` at the view boundary for every role
  allowed to perform the sensitive operation. Do not substitute baseline
  privileged-role TOTP for freshness.

## Deferred domains, live data, and AI-assisted code

The three remaining deferred `services.py` entrypoints
(`prescription.issue_prescription`, `retention.apply_retention_policy`,
`interop.exchange_clinical_record`) must continue to raise exactly
`NotImplementedError("Phase >=1")` until their owning renewal task ships. Do
not describe or expose these stubs as working clinical functionality. The
[renewal roadmap](plans/clinic-os-renewal-roadmap.md) maps each deferred
domain to its owning wave.

**AI-generated or AI-assisted code requires line-by-line human review and
targeted tests for prescription, consent, audit, RBAC, and RLS behavior before
merge.** Generated output does not relax ownership, security review, data
minimization, migration, or verification requirements. Reject invented APIs,
dependencies, provider commands, clinical assumptions, and tests that merely
mirror an implementation without proving the boundary.

Patient and scheduling work in Phase 1A is synthetic-only. A contributor may
not add live identifiers, hosted deployment claims, backup credentials, or
provider recovery steps. The unapproved templates are not evidence of approval;
every item in [LIVE-DATA-GATE.md](compliance/LIVE-DATA-GATE.md) must remain
unchecked until its named accountable owner records external approval.

## Data hygiene and review evidence

Use synthetic, non-identifying fixtures. Never include real names, national
identifiers, contact details, clinical records, credentials, database URLs,
session cookies, OTP seeds/tokens, or production telemetry in source, tests,
logs, screenshots, evidence, commits, or review messages. Keep failure messages
and audit verification evidence payload-free.

Review evidence must name the exact commit, commands, exit codes, observed
behavior, test counts, and anything not tested. A green static check does not
replace exercising the changed CLI, HTTP, or browser surface. Clean temporary
containers, volumes, servers, ports, `.terraform/`, and scratch files before
handoff.

Source anchors: [CI target](../Makefile),
[hosted workflow](../.github/workflows/ci.yml),
[module boundaries](../tests/infra/test_module_boundaries.py),
[schema policy](../tests/infra/test_schema_policy.py), and
[pre-commit hooks](../.pre-commit-config.yaml).
