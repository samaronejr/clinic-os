# apps/ - Django domain apps

## OVERVIEW
14 domain apps (~62k LOC incl. SQL-in-Python migrations). Every rule is enforced twice: Python services AND PostgreSQL (FORCE RLS, SECURITY DEFINER resolvers, triggers). Score 17 (subtree top).

## APP MAP
| App | Role | Ext import sites | Guide |
|-----|------|------------------|-------|
| tenancy | `tenant_context`, `TenantScopedModel`, `Encrypted*Field`, `TenantMiddleware` | 141 | here |
| identity | users/orgs/clinics, `UserClinicRole`, OTP, step-up, owner CLI | 244 | identity/AGENTS.md |
| core | privacy + live-halt + CSP middleware, workspace shell, `/sw.js`, outbox `integration.py`, `idempotency.py`, `secrets.py`, `fairness.py`, typed UI API `api/` (`/api/ui/v1/`), PHI-safe `telemetry.py` (JSON logs, OTel spans, `/internal/metrics`, Sentry scrub) | 49 | here |
| audit | hash-chained append-only ledger (`record_phase1_event`, `record_event`) | 94 | here |
| comms | `IntegrationOperation` outbox rows, reminders, Celery tasks, synthetic channels | 51 | comms/README.md |
| intake | patients, enrollment, contacts, patient sessions, questionnaires | 122 | intake/AGENTS.md |
| scheduling | availability, appointments, agenda, self-booking, waitlist | 131 | scheduling/AGENTS.md |
| ehr | encounters, SOAP, history, attachments | 75 | ehr/AGENTS.md |
| prescription | drafts, PDF render, signing, public verification | 43 | prescription/AGENTS.md |
| billing | invoices, synthetic PIX, payment reconciliation | 26 (tests/ops only) | billing/README.md |
| teleconsult / retention / consent | video sessions / holds+exports / teleconsult consent | 13 / 7 / 16 | own README.md |
| interop | deferred stub | 1 | - |
| orders | identity-safe orders, encrypted immutable sources, quarantine, version-bound review and audience release | new | orders/README.md |
| ai | governed synthetic inference, immutable encrypted artifacts, budgets, metering, kill switches | new | ai/README.md |

## REQUEST AUTHORITY FLOW
- `tenancy/middleware.py` wraps the WHOLE response in one durable atomic: staff -> `tenant_context(user_id, session["active_org_id"])`; `/patient/*` -> `intake.patient_access.patient_session_context` (no staff GUC). Status >=500 rolls back; streaming responses raise.
- Bypass list (no tenant txn): `/`, `/healthz`, `/readyz`, `/auth/login/`, `/static/`, `/patient/access/`, `/prescription/signing/callback/`, `/prescription/verify/`.
- GUCs are transaction-local: `app.current_user_id`, `app.current_tenant`, `app.current_patient_session`. Policies compare `NULLIF(current_setting('app.current_tenant', true), '')::uuid` -> missing context = zero rows.
- `tenant_context` raises `TenantTransactionNestingError` inside an atomic block: request code already has context; only CLI/workers/tests open it.
- Staff role gate: `identity.current_context.require_current_actor_clinic_roles`; privileged views: `identity.otp.privileged_totp_required`.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| New tenant table | inherit `tenancy.models.TenantScopedModel`; add `(table, col)` to the app's `rls.py` `*_RLS_TARGETS` allowlist; migration runs `apply_*_rls` |
| Policy/resolver/trigger SQL | `<app>/migrations/_<topic>_sql.py` constants, imported by numbered migrations via `RunSQL` |
| Encrypted column | `tenancy/fields.py` (purpose-bound, bytea), `tenancy/envelope.py` `protect`/`reveal` |
| External send/callback | `core/integration.py` `enqueue_operation`/`execute_operation`/`receive_provider_callback`; register via `register_send_adapter` + `register_subject_recheck` in `AppConfig.ready()` (comms, intake, prescription, teleconsult) |
| Subject mutation vs in-flight send | `hold_subject_mutation_lock` / `hold_subject_mutation_key` |
| Audit event | `audit/events.py` fixed matrix; payload keys only `clinic_id, http_method, http_status, object_verb, reason_code, request_id` |
| Idempotency fingerprint | `core/idempotency.py` (`clinic-idempotency-v1`) |
| Secrets/KEK | `core/secrets.py` `secret_store` (only `synthetic-file`, files mode 0600/0400) |
| Typed UI API endpoint | `core/api/views.py` (subclass `UiApiView`, POST-only, IDs in body, `@extend_schema`), serializers adapt service frozen dataclasses (`core/api/serializers.py`), route in `core/api/urls.py`; then `uv run python manage.py spectacular --file docs/api/ui-v1.yaml` (drift test `tests/infra/test_openapi_drift.py`). Refusals `{code, message_key}` via `core/api/errors.py`; bound every body scalar in the serializer so the service never sees values it cannot represent (agenda: date window, page max); unexpected exceptions become JSON `internal_error`, logged on `django.request` as type + route + frame locations only (never the message: SC-7); the `not_found` catch-all stays last in `core/api/urls.py` |
| CSP origins for a provider | `core.middleware.register_csp_extension` (connect/img/media-src, exact https/wss origins, `is_active` from provider lifecycle); never widen script/style |

## CONVENTIONS
- Per-app shape: `services.py` facade (explicit `__all__` in intake/scheduling/audit), split workflow modules, `*_views.py`, `*_presenter.py`, `rls.py`, `migrations/_*_sql.py`, `README.md` = binding domain contract; read it before editing.
- Service params keyword-only with explicit scope (`clinic_id`, `encounter_id`...); never `actor`, `request`, `organization_id`. Signatures pinned in `tests/infra/test_module_boundaries.py`, which also pins all 14 AppConfig paths.
- Deferred: `prescription.issue_prescription`, `retention.apply_retention_policy`, `interop.exchange_clinical_record` raise `NotImplementedError("Phase >=1")` (test-pinned).
- UUID PKs; frozen dataclass results; per-app `*AccessDeniedError`/`*ConflictError`; optimistic `expected_revision`/`expected_version`. Unknown vs wrong-clinic records return the same denial.
- Load-bearing private cross-app imports: `ehr.services._denied` (prescription, ehr history/attachments), `_appointment`/`_assigned` (retention), `audit.services._content_hash` (consent), `tenancy.envelope._kek` (intake search). Renaming them is a multi-app change.
- Protected-field migrations (`00xx_protected_fields.py`): `atomic = False`, resumable, irreversible.
- Billing: amounts are positive int BRL centavos (`amount_minor`); services reject float/Decimal.

## ANTI-PATTERNS
- Never query as the runtime role around RLS/ACLs; many tables have no direct `clinic_app` grants on purpose: use resolver functions.
- Never call a provider inside a transaction: prepare in tenant txn, send after commit through the outbox. Never read tenant claims from callback payloads.
- Never persist bodies, destinations, credentials, clinical text in outbox rows, `last_error` or audit.
- Telemetry allowlists live in `apps/core/telemetry.py` (`LOG_MESSAGE_ALLOWLIST` templates, args never interpolated; `LOG_FIELD_ALLOWLIST`, `SPAN_ATTRIBUTE_ALLOWLIST`, Sentry validator tables). Every emitted value is a closed-set member or validated id: route labels = registered URL names incl. namespaces, provider labels = `PROVIDER_CAPABILITY_KEYS`, AI labels = `AI_INVOCATION_PURPOSES`, logger names = imported modules. Never add an open free-text/token field; add a closed vocabulary. `/internal/metrics` needs `CLINIC_OPS_METRICS_TOKEN` + network allowlist.
- No hard deletes: DB triggers reject DELETE on appointments, audit, retention, operations, artifacts.
- Synthetic capabilities keep `real_enabled=False`; never add a setting that approves a live provider.
- Payment reconciliation (`billing/reconciliation.py`) must not run inside a request tenant transaction.

## COMMANDS
```bash
uv run --frozen --no-sync --no-env-file pytest --reuse-db -q tests/renewal/test_<feature>.py tests/infra/test_module_boundaries.py
uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner browser --suite <suite>
```
Tests: identity -> `tests/{identity,auth}`; intake -> `tests/patients`; scheduling -> `tests/scheduling`; audit -> `tests/audit`; later features -> `tests/renewal/test_*.py`.
