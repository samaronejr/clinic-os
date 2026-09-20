# OPERATIONAL TOOLING

## OVERVIEW
Container process contracts, database bootstrap/posture, and claimed test infrastructure.
Scope score: 14; distinct operational boundary with runtime entrypoints and a large imported harness.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Container purpose gate | `container/entrypoint.sh` | Accepts only `web` or `release` |
| Web process launch | `container/start.sh` | Requires `web`, then replaces itself with the command |
| Schema release | `container/release.sh`, `container/release.py` | Owner-role preflight precedes migration |
| Proxy trust override | `container/gunicorn_no_proxy.py` | Empty forwarded-IP and secure-header settings |
| Database role/schema setup | `db/bootstrap.sql` | Idempotent creation; privileges reapplied |
| First-start database initialization | `db/init/10-bootstrap.sh` | Container initialization hook |
| Runtime connection verification | `db/posture.py` | Authenticates the supplied application URL |
| Isolation and evidence harness | `testing/AGENTS.md` | Controllers, journals, schemas, and browser runners |

## CONVENTIONS
- Release requires both `CLINIC_PROCESS_PURPOSE=release` and `config.settings.release`.
- Shell purpose/setting violations exit 64; keep this distinct from application failures.
- Release uses `/app/.venv/bin/python`, not the host interpreter.
- Release order: owner assertion, `showmigrations --plan`, `migrate --plan`, migration, final check.
- The final `migrate --check` proves no unapplied migrations remain; it is not a preflight.
- `clinic_owner` owns the database and application schema; it cannot create databases or bypass RLS.
- `clinic_app` is the restricted runtime login; it receives table DML and sequence usage.
- `clinic_resolver` is NOLOGIN with BYPASSRLS; owner membership permits explicit role switching.
- `clinic_super` supplies the privileged setup/test connection, not the runtime connection.
- Bootstrap reads passwords through psql environment bindings rather than SQL literals in source.
- Posture requires an authenticated `clinic_app` URL with host, password, and database name.
- Posture also checks a separate, precreated owner-owned test database.

## ANTI-PATTERNS
- Do not run schema migration under the runtime login: the release preflight rejects it.
- Do not grant PUBLIC database/schema access; bootstrap explicitly revokes it.
- Do not give `clinic_app` superuser or BYPASSRLS privileges to make a test pass.
- Do not collapse web startup and release into one ungated process path.
- Do not treat a reachable PostgreSQL socket as proof of the required database posture.

## RELATED CHECKS
- `tests/test_container_contract.py`: entrypoint, image, and release contracts.
- `tests/test_db_posture_url.py`: validation of the actual runtime database URL.
- `tests/test_schema_policy.py`: database ownership and schema policy.
