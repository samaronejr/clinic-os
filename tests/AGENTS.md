# TEST KNOWLEDGE BASE

## OVERVIEW
Cross-domain regression contracts and shared harnesses; score 12 earns this distinct test-infrastructure domain its own guidance.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Global fixture registration and DB teardown | `conftest.py` | Re-exports RBAC fixtures; wraps transactional teardown |
| Multi-clinic actors and memberships | `rbac_fixtures.py` | `RbacGraph` spans two organizations and three clinics |
| Owner/runtime connection targeting | `database_urls.py` | Replaces only the DB-name URL path, with percent encoding |
| Appointment races | `appointment_concurrency_support.py`, `test_appointment*concurrency.py` | Worker-local connections, barrier and lock coordination |
| HTTP/service setup | `*_http_support.py`, `*_service_support.py` | Reuse domain payload, actor and seed helpers |
| Audit SQL and ledger contracts | `phase1_audit_*_support.py`, `test_audit_append.py`, `test_audit_ledger.py` | Catalog, ACL, hash-chain and tamper assertions |
| Tenant context and RLS | `test_tenancy_rls.py`, `test_tenant_guc_boundaries.py`, `test_tenant_middleware.py` | Missing, malformed and cross-tenant context |
| OTP and freshness setup | `otp_test_support.py`, `stepup_test_support.py` | Device, session and privileged-access cases |
| Isolation state-machine setup | `isolation_*_fixtures.py`, `test_isolation_*.py` | Durable claims, receipt lineage, replay and recovery |
| Normative JSON vectors | `fixtures/isolation/normative/`, `test_isolation_normative_schemas.py` | Schemas live in `ops/testing/*.schema.json` |
| Browser adapter contract | `browser/test_runtime_https.py` | Fake retained contexts; not a real HTTPS browser run |
| Shared browser assertions | `browser/visual_contract.py` | Re-export of `ops.testing.browser_visual_contract` |
| Process/Make invocation contracts | `makefile_process_probe.py`, `test_makefile_security.py` | Process arguments, environment transport and redaction |

## CONVENTIONS
- Most tests remain flat under this directory; helpers import as `rbac_fixtures`, not `tests.rbac_fixtures`.
- `browser/` is a package; do not infer that the top-level test directory is one.
- `conftest.py` explicitly exports `RbacGraph` and `rbac_graph` via `__all__`.
- Use graph fixtures for cross-organization and cross-clinic cases; preserve their distinct identities.
- Transactional DB suites commonly set `pytestmark = pytest.mark.django_db(transaction=True)`.
- `app_database_url` and `superuser_database_url` retarget configured URLs to Django's current test database.
- Their environment inputs are `APP_DATABASE_URL` and `TEST_SUPERUSER_DATABASE_URL` respectively.
- Transactional teardown temporarily disables immutable audit triggers for cleanup, then restores and verifies them.
- `audit_triggers_finally_enabled` separately verifies session-final trigger restoration when requested.
- Appointment worker helpers close old connections before work and all worker connections in `finally`.
- Coordinate races through barriers, queues and observed PostgreSQL lock state, not elapsed-time assumptions.
- Assert both business outcomes and persistence boundaries: rollback, exact-once audit, RLS and role visibility.
- Normative schema tests require Draft 2020-12 and recursively closed objects.
- Most normative domains pair `valid.json` with `invalid-*.json`; ledger claims and final-input-freeze use different layouts.
- Browser runtime tests assert retained-context reuse, `/readyz`, page closure and fixed artifact ordering.
- Executable visual assertions live under `ops/testing/` because the runner image ships that tree, not these tests.

## ANTI-PATTERNS
- Do not leave immutable audit triggers disabled; teardown checks that tests started with them enabled too.
- Do not replace runtime-role or cross-connection assertions with owner-only ORM success cases.
- Do not redirect raw SQL helpers to the original configured database instead of Django's test database.
- Do not loosen closed schemas to make negative fixtures pass; committed malformed vectors must remain rejected.
- Do not treat the fake-page runtime contract as evidence that Playwright, TLS or the browser runner executed.
- Do not move executable browser assertions solely into `tests/`; they would disappear from the runner image.
- Do not copy the large audit suites as setup templates; use the dedicated support modules for shared machinery.
