# TENANT TRANSACTION BOUNDARY

## OVERVIEW
Request/command tenant scope backed by durable transactions and forced RLS; score 9 for the central isolation boundary.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Authorized transaction lifetime | `db.py` | `tenant_context`, cleanup, typed boundary errors |
| Signed-session request scope | `middleware.py` | `TenantMiddleware`, public bypasses, error rollback |
| Tenant-aware commands | `management.py` | `TenantCommand` |
| Shared tenant model base | `models.py` | `TenantScopedModel`, isolation probe |
| Approved RLS DDL | `rls.py` | Fixed table/column allowlist |
| Resolver SQL and policy installation | `migrations/0002_rls_and_resolvers.py` | Tenant membership/resolver boundary |
| Regression coverage | `../../tests/test_tenant_*.py` | GUC poisoning, request rollback, streaming rejection |
| Model isolation coverage | `../../tests/test_identity_isolation.py` | Cross-tenant visibility |

## CONVENTIONS
- `tenant_context(user_id, org_id)` must own the outermost transaction.
- It rejects entry while `connection.in_atomic_block` is already true.
- The user GUC is set before `clinic_app.user_has_org` validates membership.
- The tenant GUC is installed only after successful membership validation.
- GUC assignment is transaction-local; final cleanup also resets persistent connection state.
- Cleanup does not open a previously unused database connection.
- Middleware parses user/session organization IDs before evaluating downstream work.
- Public bypasses include landing, health/readiness, login and static paths.
- Bypasses clear stale GUCs before and after their downstream handler.
- Tenant responses with status 500 or higher mark the transaction for rollback.
- RLS helpers enable and FORCE policies with both `USING` and `WITH CHECK`.

## ANTI-PATTERNS
- Do not nest `tenant_context` inside a caller-owned atomic block.
- Do not return streaming tenant responses; deferred work outlives the transaction.
- Do not retain lazy tenant ORM work for execution after context exit.
- Do not let a bypass or malformed session inherit prior connection GUC values.
- Do not expand raw RLS SQL to arbitrary table names; extend the fixed allowlist deliberately.
- Do not confuse tenant membership validation with exact-clinic role authorization.
