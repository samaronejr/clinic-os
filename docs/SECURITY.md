# Clinic OS Phase 1A security model

This is the implemented Phase 1A synthetic-data security contract. It explains what the
controls do and, equally importantly, what they do not do. Operational steps
are in [RUNBOOK.md](RUNBOOK.md); architectural context is in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Database roles and posture

The bootstrap creates four roles with distinct duties:

| Role | Login | Superuser | `rolbypassrls` | Intended use |
| --- | --- | --- | --- | --- |
| `clinic_owner` | yes | no | no | database/schema owner and Django migration connection |
| `clinic_app` | yes | no | **no** | ordinary runtime connection; it is not the database or schema owner |
| `clinic_resolver` | no | no | yes | narrowly privileged owner of fixed resolver/auth `SECURITY DEFINER` functions |
| `clinic_super` | yes | yes | no (superuser still bypasses RLS) | local/CI database administration and test setup only, never application runtime |

`clinic_app` is `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`. It receives
the minimum tenant-table DML needed by the foundation, cannot log in as
`clinic_resolver`, and has no direct table or column privilege on
`identity_user`.

`clinic_resolver` is `NOLOGIN BYPASSRLS`. It has narrowly enumerated reads on
the identity membership tables and owns four fixed-SQL functions:
`user_has_org`, `user_organizations`, `auth_lookup`, and `load_current_user`.
It has schema `USAGE, CREATE` only so an owner-controlled migration can
`SET ROLE clinic_resolver` and create those resolver-owned functions; there is
no credential with which an application process can assume it directly.
Each is `SECURITY DEFINER`, pins `search_path` to trusted schemas, uses no
dynamic SQL, accepts no arbitrary-user argument, and is revoked from `PUBLIC`.
Only the exact execute surface needed by `clinic_app` is granted. Bypass RLS is
therefore contained behind reviewed functions rather than exposed to a runtime
credential.

## Tenant boundary and fail-closed order

`TenantMiddleware.__call__` owns an outermost transaction for each tenant
request. It validates the signed session identifiers, then:

1. sets transaction-local `app.current_user_id`;
2. validates the selected organization with `user_has_org(org_id)`, whose user
   identity comes from that GUC;
3. sets transaction-local `app.current_tenant` only after membership passes;
4. executes lazy session-user rehydration and all domain work before the
   transaction closes;
5. resets both GUCs on normal, rejected, and exceptional exits.

Every current tenant table has both `ENABLE` and `FORCE ROW LEVEL SECURITY`.
The single permissive policy applies to all commands and uses the same
`USING`/`WITH CHECK` predicate. `NULLIF(current_setting(..., true), '')`
ensures an unset or empty tenant fails closed. Tests enumerate the complete
tenant-model/policy set, so a new tenant table must update and pass that drift
check before merge.

### Threat boundary

Transaction-local app-set GUCs, membership validation, FORCE RLS, and the
trusted-context append API protect against accidental tenant leakage and
cross-tenant appends by ordinary application code using `clinic_app`. They are
defense in depth against application mistakes; they are **not** a defense
against a fully compromised PostgreSQL superuser, database host, or migration
owner. A sufficiently privileged database attacker can change roles, functions,
policies, rows, or server state. Independent external audit anchoring is
deferred to a later wave and is required to detect tampering beyond this trust
boundary.

## Session authentication and identity writes

Phase 1A screens use Django session authentication and require authentication by
default. There are no token or service-account authentication modes. The
custom backend calls `auth_lookup` before session creation and
`load_current_user` for GUC-bound session rehydration; runtime SQL cannot read
`identity_user` directly.

Phase 1A deliberately disconnects Django's `update_last_login` receiver, so
login does **not** maintain `last_login`. The read-only runtime boundary also
means `request.user.save()`, password reset, admin user editing, and ordinary
runtime user creation are unsupported. User creation and password mutation
must use an owner/migration-controlled process today or a later, narrowly
scoped mutation `SECURITY DEFINER` function with dedicated review and tests.
Do not grant `clinic_app` write access to `identity_user` as a shortcut.

Signed sessions are invalidated by normal Django logout. Password login clears
any previous OTP device and recent-verification state before establishing the
new session.

## TOTP and recent-verification step-up

Privileged foundation routes require a confirmed django-otp TOTP device bound
to the authenticated user. TOTP secrets and submitted tokens are marked as
sensitive, excluded from telemetry, and never belong in logs, issues, fixtures,
or documentation.

For an operation-specific step-up, `assert_step_up(request)` or
`require_recent_verification()` enforces a default 300-second window. The
boundary is inclusive: age 0 through 300 seconds is accepted; age 301 is not.
It requires all of the following:

- an authenticated, active project `User` that django-otp reports verified;
- a confirmed TOTP device owned by that exact user;
- an exact match between the session's persistent device identifier and the
  current device;
- an integer `otp_verified_at` that is neither in the future nor older than the
  configured non-negative integer maximum age.

Missing, boolean, non-integer, malformed, future, or stale freshness fails
closed. Invalid user/device state also clears the device binding. A malformed
maximum-age argument fails closed. Successful enrollment, ordinary TOTP
verification, and re-verification rotate the session key, bind the confirmed
device, and stamp fresh verification time.

The `/auth/step-up/` challenge accepts GET/POST, uses CSRF protection, applies
the existing OTP throttling/replay checks, rejects users without a confirmed
device, and sanitizes `next` to a same-host destination outside authentication
flows. Standard redirects use 302; valid HTMX redirects use 204 plus
`HX-Redirect`. Authentication responses are private, no-cache, no-store,
must-revalidate, and vary on the HTMX request header.

The step-up API is a reusable extension point only. Prescription issuance,
consent capture, document issuance, and other sensitive product operations are
not implemented in Phase 0 and must not be represented as available.

## Audit ledger

`record_event` accepts a bounded semantic event and only five payload keys:
`http_method`, `http_status`, `object_verb`, `reason_code`, and `request_id`.
Sensitive clinical or identity fields are rejected rather than placed in the
ledger. Organization and actor are trusted context derived from
`app.current_tenant` and `app.current_user_id`; callers cannot supply a
different tenant to `audit_append`.

The content hash is versioned and domain-separated: SHA-256 covers the
`clinic-audit-v1` domain plus RFC 8785 canonical JSON. PostgreSQL serializes
each organization chain and hashes the content hash together with the previous
link. The ordinary append function requires valid tenant and actor GUCs. The
separate all-zero system chain is owner-only. Ordinary runtime code can select
only the tenant-filtered view; direct insert/update/delete/truncate privileges
are revoked, and triggers reject UPDATE, DELETE, and TRUNCATE even for relation
paths that reach the table. `verify_chain()` replays both semantic and link
hashes and emits payload-free failure evidence.

External anchoring, immutable off-database storage, and alert delivery are not
implemented. Treat chain verification as an integrity check inside the stated
database trust boundary, not as proof against a superuser.

## Secrets, encryption, and telemetry

- Copy `.env.example` only as a schema for local variables. Replace every
  placeholder locally; never commit `.env`, database URLs, credentials,
  session keys, OTP seeds, tokens, patient identifiers, or clinical data.
- Production settings require an explicit Django secret key. Sentry is inert
  without a DSN and is configured with PII and local-variable capture disabled.
- The Terraform skeleton requests encrypted RDS storage and provider-managed
  master-password rotation, but it has not been applied. TLS termination,
  application-field encryption, KMS policy, key rotation, and restoration are
  deployment-specific hooks requiring an approved production runbook.
- Use synthetic, non-identifying values in tests and evidence. Audit payloads
  are metadata, not a place for PHI.

## Synthetic recovery and live-data gate

`make restore-rehearsal` transports only the reviewed table/sequence manifest
between two task-owned PostgreSQL 16.14 containers. It verifies the archive
hash and TOC before target mutation, migrates the target first, restores no
framework/session/migration state, and compares restored domain rows and
posture read-only. `clinic_super` is confined to exact task database creation
and data transport; source and target PostgreSQL clients run inside their own
containers. The rehearsal executes no restored bootstrap, provisioning,
password, login, TOTP, helper, browser, or runner action.

This is not an encrypted backup, provider PITR, RPO/RTO, or recovery approval.
Live data is prohibited until every item in
[LIVE-DATA-GATE.md](compliance/LIVE-DATA-GATE.md) is independently approved.

## Incident response

Report suspected tenant leakage, credential exposure, authentication bypass,
OTP compromise, audit-chain failure, or unexpected database-role drift to the
maintainers immediately through the private incident channel. Maintainers must
acknowledge and triage a report within **three business days**; active exposure
or patient-safety risk requires immediate containment rather than waiting for
that service target.

Containment priorities are: preserve evidence without copying PHI into tickets,
disable affected credentials or traffic, capture current role/policy/function
posture, verify the relevant audit chains, and identify the earliest trusted
restore point. Rotation and recovery actions require the authorized provider
or deployment runbook; do not improvise destructive database commands. Record
the timeline, scope, decisions, and follow-up tests in a restricted incident
record.

Source anchors: [database bootstrap](../ops/db/bootstrap.sql),
[RLS policy builder](../apps/tenancy/rls.py),
[resolver migration](../apps/tenancy/migrations/0002_rls_and_resolvers.py),
[authentication backend](../apps/identity/auth_backends.py),
[step-up policy](../apps/identity/stepup.py),
[canonical audit content](../apps/audit/canonical.py), and
[append migration](../apps/audit/migrations/0002_audit_append.py).
