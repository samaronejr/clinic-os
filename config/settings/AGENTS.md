# SETTINGS KNOWLEDGE BASE

## OVERVIEW
Import-time runtime, release, and browser trust contracts; score 9, a distinct settings-validation domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Shared defaults and uppercase export | `base.py` | `export_settings()` copies uppercase globals only |
| Local dotenv loading | `dev.py` | Reads checkout `.env` before importing base |
| Test database connection | `test.py` | Uses migration-owner URL; MD5 password hasher |
| HTTP runtime | `prod.py` | App-role database; HTTPS redirect and secure cookies |
| Migration/release commands | `release.py` | Owner-role database; removes `WSGI_APPLICATION` |
| Secret, host, synthetic-mode validation | `contracts.py` | Raises `ImproperlyConfigured` without echoing input |
| Strict PostgreSQL URL parser | `database.py` | Exact query keys, verified TLS, canonical CA path |
| Browser process settings | `browser.py` | Closed environment plus reserved RPC attestation |
| Browser environment shape | `browser_contract.py` | Exact key set and project-bound database |
| Browser root/claim/process checks | `browser_authority*.py`, `browser_process_contract.py` | Separate authority and process contracts |
| Browser socket response validation | `browser_rpc.py` | Canonical response, peer credentials, bounded freshness |
| Sentry filtering | `telemetry.py` | Removes request cookies, body, headers, query string |

## CONVENTIONS
- `dev.py` and `test.py` inherit via wildcard import; do not assume all variants do.
- `prod.py`, `release.py`, and `browser.py` call `base.export_settings(globals())` before overrides.
- Production reads `APP_DATABASE_URL` and accepts only `clinic_app`.
- Release reads `MIGRATION_DATABASE_URL` and accepts only `clinic_owner`.
- Strict database URLs require explicit port, `sslmode=verify-full`, and timeout 1-3 seconds.
- The CA must be a readable, canonical absolute regular file owned by the effective UID/GID.
- Browser imports require a private Unix socket and a valid reserved process claim, not just env vars.
- Browser HTTP deliberately disables redirect/secure cookies; this is not the production profile.
- Direct static storage is selected only for exact browser/dev/test module names.
- Other settings modules use compressed manifest storage; test mode alone enables WhiteNoise autorefresh.

## ANTI-PATTERNS
- Do not serve WSGI with release settings or reuse the migration owner for production HTTP.
- Do not enable proxy-header trust: prod/release/browser set `SECURE_PROXY_SSL_HEADER = None`.
- Do not loosen the database query allowlist or allow a symlinked CA to bypass path checks.
- Do not treat browser settings as a convenient alternate local development profile.
- Do not include secrets or database URL values in contract failure messages.
- Do not enable Sentry local-variable capture or request-body collection.

## CONTRACT TEST MAP
- Secret/host/HTTPS settings: `tests/test_production_settings.py`.
- Release role and non-WSGI boundary: `tests/test_release_settings.py`.
- Database defaults/options: `tests/test_database_settings.py`, `tests/test_database_options.py`.
- Browser environment/authority/RPC: `tests/test_browser_settings.py`.
- Shared startup timezone enforcement: `tests/test_timezone_source.py`.
