# KEK binding — defensive verification

## Successor: P1 role posture and retained SC-5

The product branch now points to `6b84a4faf05183d3ac622d212c51bbdeb58987f6` (tree `491dd5a2d4955c18d3242cd3f00a86bb0747e6f0`), one commit after
the original binding fix. The independent code review at `76376101` found
that checking login OIDs could miss a privileged, non-inherited role reachable
via `SET ROLE`. The previous full-run totals also lacked retained per-case
primary evidence. The historical claim below is unchanged except for this
superseding status; it is **not** the evidence for the successor.

**Production fix.** `ops/db/posture.py` now walks the complete membership
graph of every covered login. The graph is deduplicated with recursive UNION
and is used for both role settings and privilege checks in one repeatable-read,
read-only catalog snapshot. It rejects reachable superusers; delegated SET
or ALTER SYSTEM on any parameter ACL (including PUBLIC); monitoring, server
file and server program roles; and effective EXECUTE on every built-in
overload of pg_read_file, pg_read_binary_file, pg_ls_logdir and lo_import.
This is conservative even for a membership edge currently not usable. No
runtime query, encrypted field, tenant GUC or outbound workflow was changed.

**Negative evidence.** A real two-hop SET-only grant made the deploy verifier
refuse while the direct `clinic_app` parameter-privilege check remained false;
revoking the membership restored success. Thirteen additional transaction-
rolled-back grant cases cover the protected powers and inherited-after-switch
chains, without executing privileged server operations. Removing the recursive
edge caused the end-to-end posture regression to fail. Disabling binding only
on the tagged producer separately failed the public-canary assertion before
any secret-bearing SQL. The positive focused gate passed 33/33.

**Single-run evidence.** Exact-SHA unit `cpi-kek-sc5-full-6b84a4fa.service`, invocation
`d3092b07d8984d0e84a487a29c19c5af`, exit 0, Nice=10. 16,632 separately collected non-browser
IDs: all 16,618 original IDs and 14 new P1 cases. All 16,632 had one passing
setup, body, teardown and one JUnit case in this invocation: missing, duplicate,
extra, skipped, failed and errored counts all zero. Coverage 93.59%.
The primary JUnit is retained without properties, captured output, exception
text or raw parameter IDs; this is not a reconstruction from counts or retries.
Manifest: `SC5-6b84a4fa/outcomes.jsonl`, SHA-256 `475a62d54cea8370b398a10c5a258d8049f1847a5737fda6b5def7cd8bc22d65`. JUnit:
`SC5-6b84a4fa/junit.xml`, SHA-256 `30039f4077ee38967b5404b84b5f94fae31c5888e247c02ca096ff57e28fd8fa`. `SC5-6b84a4fa/README.md` and the
included reporter, reference encoder and stdlib verifier document exact
identity and duration semantics. `SC5-6b84a4fa/SHA256SUMS` seals all artifacts.

**Other gates.** On this exact code commit: Ruff, formatting and strict mypy
(2,949 files), `manage.py check`, `makemigrations --check --dry-run`, bootstrap,
migration, `db-posture` and `git diff --check` passed. The independent ops/testing
isolation harness passed 3,308/3,308 with the same ID set as the full run,
including all six restore/isolation guard tests. Both runtime aliases retain
server-side binding; the previously documented offline administrative literals
are outside the runtime claim. This is synthetic evidence, not review approval.

**Execution and cleanup.** New disposable PG16.14 and Redis7; the verified
Redis 7.4.11 binary was on PATH, broker gate and Poppler required, umask 022.
On the lane cluster only, fsync was disabled at 2026-11-24T20:52:13.994305Z
after sustained I/O pressure and DataFileImmediateSync waits; cluster removed
by 2026-11-25T00:51:31+00:00. No disabled truncate guards (126/126); no other client
backends. Both named volumes/containers absent, ephemeral loopback ports
rebound and released. Publication uses distinct code and evidence-only refs,
`ulw/cpi-h-kek-binding` and `ulw/cpi-h-kek-binding-evidence-sc5`, so the retained manifest does not change the code SHA.

## Historical `76376101` record

**Status: pushed, independently read back, and lane resources removed.**
Branch `ulw/cpi-h-kek-binding`, based on `4f8d2454987ca1d083ead53a7501126d647a89bc`.
First candidate: `76376101614f209d9d7b4ab4a74ac87fdca4f832`.
Synthetic data only; no live readiness, hosted CI, or external service claim.

## Baseline and scope of the evidence

The baseline Python encryption calls already parameterize the SQL. The secret
store supplies the KEK to `apps/tenancy/envelope.py` (`_kek`, `_call_scalar`,
tenant/protected encrypt/decrypt, issue, blind-index and rewrap paths).
`apps/comms/inbox_inbound.py::inbound_context` uses a proof in both a resolver
call and a transaction-local GUC write. The baseline Django connection builders
set `prepare_threshold=None`, but not `server_side_binding=True`. On the pinned
Django 5.2.17 / psycopg 3.3.4 this selects `Cursor` → `psycopg.ClientCursor`,
**not** `ServerBindingCursor` → `psycopg.Cursor`.

No historical KEK was extracted, hashed or included in evidence. The baseline
claim is a source/driver trace, backed by a controlled client-binding mutation:
the second authenticated `clinic_app` connection checked for an **exact
placeholder template**, never retrieved the other connection's SQL. This is a
public-canary contrast, **not** an observation or hash of a leaked KEK.

## Change and query inventory

* `config/settings/database.py::postgres_options()` produces fresh options:
  server-side binding **on**, named prepared statements still **off**, same
  search path. Base uses it for default and the separately built locks alias;
  dev inherits base. Test and renewal use it before copying the aliases. The
  strict parser supplies prod default/locks/agent, release owner, browser
  default/lock copy. The non-TLS agent constructor uses it too. The realtime
  settings inherit the selected parent; lock factories inherit their named
  alias. No `ATOMIC_REQUESTS`, cursor pooling, or transaction-lifetime change.
* Application envelope, webhook resolver and webhook `set_config` SQL
  themselves are **unchanged**. No function rewrites, role impersonation or
  changes to `apps/comms` send outcomes.
* `tests/identity/test_identity_acl_migration.py`: replace the two
  parameterized `SET app.current_*` statements with one scalar select of
  two `set_config(..., value, false)` calls. False preserves the original
  **session**, not transaction, lifetime.
* `tests/scribe/test_draft_authority.py`: type the polymorphic JSON builder's
  citation ID as `text`; assert the **actual** `invalid draft source`
  integrity error, rather than accepting any driver/database error.
* `ops/db/bootstrap.sql`: revoke ordinary SET on the protected logging GUCs;
  apply zero non-error and on-error limits and `terse` verbosity as **login
  defaults** to the canonical app/owner/agent and existing optional machine
  logins. `ops/testing/https_role_bootstrap.py` installs the same defaults
  for the HTTPS stack's three fresh, separately created logins.
* `ops/db/posture.py`: verify fresh-session effective settings, supported
  stderr/syslog destinations, absence of protected SET privileges, and
  exact applicable global/role/database settings (including separately
  provisioned machine logins). New logins need bootstrap + posture. Existing
  pool/server sessions must reconnect. CSV/JSON preserve separate error
  context fields and are explicitly **not** accepted by this posture.
* Settings, HTTPS-bootstrap and pooling tests pin the resulting option maps
  and catalog rows. `tests/infra/test_bound_parameters.py` compares nine
  exact templates: public canary, both tenant-context setters, tenant
  encrypt/decrypt, protected encrypt/decrypt, webhook resolver, webhook
  proof/scope setter. The writer waits for the reader's result before its
  next SQL; synchronization uses an Event and a joined executor. The reader
  gets only a boolean for a uniquely tagged, idle or idle-in-transaction
  backend of the same database/role. The test never emits query bodies.

### SQL statement checklist for `76376101`

There were **two binding-compatibility call sites**, both in tests.
No production envelope or webhook query text was changed.

| Site | Changed or added SQL |
|---|---|
| Identity migration fixture | Replaced `SET app.current_user_id = %s` and `SET app.current_tenant = %s` with one `SELECT set_config('app.current_user_id', %s, false), set_config('app.current_tenant', %s, false)`. |
| Scribe citation guard fixture | Its `INSERT … SELECT` now uses `jsonb_build_object('kind','transcript','id',%s::text)` in `jsonb_build_array`; only the citation ID argument changed. |
| Main bootstrap | Added `REVOKE SET ON PARAMETER log_parameter_max_length, log_error_verbosity` for PUBLIC and the three named login roles. A `SELECT format(...)` over matching login names emits three `ALTER ROLE … SET` statements: `log_parameter_max_length=0`, `log_parameter_max_length_on_error=0`, `log_error_verbosity=terse`. |
| HTTPS bootstrap | Added the same three `ALTER ROLE … SET` statements to the existing bootstrap batch, for each of its app, agent and owner logins. |
| Deployment posture | `POSTURE_QUERY` adds sensitive `SET` privilege checks and the stderr/syslog destination check. New `MACHINE_LOGINS_QUERY` selects optional machine logins. `EFFECTIVE_SETTINGS_QUERY` adds all three logging GUCs to its `current_setting` results. Existing `ROLE_SETTINGS_QUERY` text is unchanged; its bound role list now includes the machine logins. |
| Pooling and HTTPS tests | Pooling adds the same three `current_setting` results and passes the expanded role set to the existing catalog query. HTTPS adds `SELECT rolname, rolconfig FROM pg_roles WHERE rolname = ANY(%s)`. |
| Binding regression | Adds `SELECT %s::text` (public canary), and one boolean-only equality check of the expected statement template in `pg_stat_activity`, restricted to the uniquely tagged same-role test backend. The nine templates are *expectations* for actual calls, not nine rewritten production statements. |
| Privilege regression | Adds `SELECT session_user, current_user`; two denied attempts (`SET log_parameter_max_length = -1`, `SET log_error_verbosity = verbose`); one rollback-only successful `SET LOCAL log_parameter_max_length_on_error = -1`; and a `SELECT current_setting(...)` of the non-error limit, on-error limit and server verbosity. |

## PostgreSQL control proof

Actual test server: PostgreSQL **16.14**, Python 3.12.3. `clinic_app` saw
`log_parameter_max_length=0` (context `superuser`),
`log_parameter_max_length_on_error=0` (`user`), and `log_error_verbosity=terse`
(`superuser`). The GUCs **do not have identical privileges**. Separate fresh
runtime connection: raising the non-error limit or server verbosity fails
with SQLSTATE `42501`; `SET LOCAL` of the on-error limit to `-1` succeeds and
is rolled back. The latter cannot honestly be tested as permission denied.
The compensating control excludes bind context from supported log destinations.
Deliberate privileged changes to those controls, untrusted extensions, and
queries explicitly emitting argument values are outside this core bind-logging claim.

**Who can affect whom:** two concurrent, independently authenticated, distinct
`clinic_app` backends reported on-error limits `(0, 0)`. A `SET LOCAL` of
one byte in the first yielded `(1B, 0)`; a third fresh login still reported
`0`. After rollback the pair was `(0, 0)` again. This proves isolation of
session `SET`, **not** immutability of login defaults: a rollback-only
`ALTER ROLE CURRENT_USER SET ... = 0` also succeeded. A committed change to
a USERSET role default could affect *future* same-role logins, not an
already-open backend. Even then both the protected non-error limit and
protected server-side `terse` control still apply. Posture rejects changed
defaults. See PostgreSQL 16 manual sections *ALTER ROLE*, *Error Reporting
and Logging* and *System Administration Functions*.

**Reading logs is a different privilege:** on the actual instance,
`clinic_app` was not superuser/CREATEROLE/BYPASSRLS and had **zero** other
role memberships/SET targets. `MEMBER`, `USAGE` and `SET` were false for
`pg_read_server_files`, `pg_execute_server_program`, `pg_write_server_files`,
`pg_read_all_settings` and `pg_monitor`. Effective `EXECUTE` was false for
**all** overloads of `pg_read_file` (4), `pg_read_binary_file` (4),
`pg_ls_logdir` (1) and `lo_import` (2). No foreign tables were installed.
Actual destination was external stderr, in-server collector off. An ordinary
SQL login does not acquire the Docker/host log reader. These assertions
checked **effective ACLs**, not the contents of any log file. This is not
a claim about someone separately granted host/root/central logging access.
The separate operator-only harmless log-canary test below establishes that
bind values were not written; it does not rely merely on lack of log access.

**Separate administrative follow-up, not protected by this Django change:**
`ops/testing/restore_verification.py::{require_key_probe,verify_object_store}`
construct escrow-key literals for the offline `ContainerPostgres.sql` psql
transport (`clinic_super`, not a Django `clinic_app` cursor). Core bind-detail
limits do not redact literals. A separate transport change should pass keys
through an extended-protocol bind inside the owned restore stack. This lane
did not change restore authority/protocol or claim that restoring via psql
protects the admin query text. No escrow key was retrieved.

A disposable-cluster operator changed `log_statement`, the duration threshold,
the duration-sampling threshold, and transaction sample rate while the
application held a transaction. The typed public probe ran in that transaction
and the next; enabled modes actually produced log records. A deliberate
division-by-zero with a harmless parameter and runtime on-error limit `-1`
actually produced an error log. No canary value occurred in server logs. Each
operator setting was reset. **Public log-canary SHA-256** (not a key):
`ce7820f1578c04d0f02316c6967d819e7d72d5eece66591e29469253d585fc8e`.

## Gate receipts (UTC)

All long runs were waited on through `systemd-run --user --wait --collect`
units `cpi-kek-*` with `Nice=10`. One DB pytest at a time. Container IDs and
credentials are not needed to reproduce the test suite and are omitted.
Only `cpi-kek-pg-01a20d6b` (internal port 55432, published loopback 33294)
and `cpi-kek-redis-01a20d6b` (56379 → loopback 33295) were started.
The shared host showed full IO pressure ~29% over 10 seconds. **Only** the
disposable PG container started with `fsync=off` at
`2026-11-24T12:08:22.535867Z`; confirmed as the runtime role.

* `make db-bootstrap`, `make migrate`, `make db-posture`: exit 0. Fresh install.
* `pytest --reuse-db -q tests/infra/test_bound_parameters.py tests/realtime/test_pooling.py tests/infra/test_agent_database.py tests/infra/test_database_options.py tests/identity/test_identity_acl_migration.py tests/scribe/test_draft_authority.py tests/identity/test_tenant_guc_boundaries.py`: 49 passed, exit 0.
* `pytest --reuse-db -q tests/infra/test_https_role_bootstrap.py tests/infra/test_db_posture_url.py tests/scribe/test_draft_authority.py::test_raw_citation_and_history_guards_reject_foreign_references tests/isolation/test_backup_restore_guard.py`: 14 passed, exit 0.
* Final nine-template test, role defaults and typed scribe guard: `pytest --reuse-db -q tests/infra/test_bound_parameters.py tests/realtime/test_pooling.py tests/scribe/test_draft_authority.py::test_raw_citation_and_history_guards_reject_foreign_references`: 8 passed. The external mutation patched only the producer's options at pytest call time (after encrypted fixture setup), reopened it, and reran the same binding node. **1 failed**, exit 1 as required, first mismatch `SELECT $1::text`; no encryption/proof operation executed on the mutated connection.
* Candidate `76376101`, `cpi-kek-static-76376101`, start `2026-11-24T12:27:28Z`: `ruff check .`, `ruff format --check .` (2,948 files), `mypy .` (strict, 2,948 files), `manage.py check`, `manage.py makemigrations --check --dry-run`, `make db-posture`, `git diff --check 4f8d2454..HEAD`: all exit 0.
* Invalid preliminary full-run launchers are **not** counted: systemd expanded a shell array before the first invocation; xunit2 caused warnings-as-errors with `record_property` in the second. Each was stopped, verified quiescent, and the lane's test DB was rebuilt from bootstrap/migrations. **126/126** expected guards enabled before rebuild and after, none missing or disabled.
* First completed exact-commit non-browser run, `2026-11-24T12:32:44Z` to `16:11:03Z`, exit 1: **16,577 passed, 38 setup errors, 3 failed, 0 skipped**, 93.43% coverage. The two new regression tests, 14 recovery tests, 6 restore/isolation guard tests and HTTPS bootstrap test all passed. The 38 setup errors required `redis-server` on `PATH`; an endpoint alone does not provide the fairness fixture executable. The 3 harness tests try to change ordinary file/directory modes to 0600/0700, but with a process umask of 077 those mutations did nothing. Independent fake-only harness likewise reported 3 failed / 3,305 passed. All 126 truncate guards remained enabled at suite exit; no application SQL incompatibility appeared in this run.
* Corrected **test environment only**, not source: supplied a lane-private Redis 7.4.11 executable from the local official `redis:7` image (binary SHA-256 `acf84fdf1c977253714617a7470b0acf5237c4b0bc1b36c3842b332c8dce491b`). Logs and workspace roots remain private (0700 directories); pytest inherits ordinary umask 022 so mode-drift mutators exercise actual changes. All 41 affected cases selected from the previous JUnit and then the complete exact-SHA suite, plus the independent fake-only isolation harness, were rerun.
* Corrected environment: **41/41 passed** in 65.14 s with no skips/errors; **126/126** guards enabled. Independent ops/testing fake-only harness `pytest -q -p no:cacheprovider --basetemp=<private> -o junit_family=xunit1 --junitxml=<private>/isolation-r2.xml tests/isolation`: **3,308/3,308 passed** in 365.64 s; no failures, errors or skips. The earlier `session-privilege` scratch proof expected `1` rather than PostgreSQL's actual formatted `1B`; corrected display expectation, `session-privilege-r2` exit 0, no source change.
* Independent collection on clean tree `e6ca823b183ff1f3e7dd6c5bcafa0a1ff4ec994a` at candidate SHA: `pytest --collect-only -qqq -p no:cacheprovider -p independent_ids --ignore=tests/renewal/browser tests`, exit 0, 16,618 distinct full pytest node IDs and 16,618 distinct `(file, classname, name)` JUnit identities. No raw IDs retained in the claim. The final oracle compares **multisets**, not just counts, including class and parameter variants; preliminary red runs do not contribute results.
* Final exact-commit gate `cpi-kek-final-gate-76376101`, Nice=10, systemd invocation `461b24c0b13e4688986ae8535ea69f18`: `ExecMainCode=CLD_EXITED`, `ExecMainStatus=0`, `Result=success`. From **2026-11-24T16:15:22+00:00** through **2026-11-24T19:54:59+00:00**, on `76376101614f209d9d7b4ab4a74ac87fdca4f832`, clean tree `e6ca823b183ff1f3e7dd6c5bcafa0a1ff4ec994a` at entry and unchanged at exit. Command: `python -m pytest -q --tb=short --reuse-db --ignore=tests/renewal/browser -o junit_family=xunit1 -p lane_report <all 26 --cov targets from ops/testing/coverage-targets.txt> --cov-fail-under=90 --cov-report=term:skip-covered --junitxml=<private>/final-gate-76376101.xml tests`. `lane_report` records only failed test locations and error types, never alters selection or outcomes.
* **16,618 passed; zero failures, errors or skips**. Independently collected vs executed: **16,618/16,618**; missing 0, extra 0, duplicate 0, including parameter variants. Sorted reference and JUnit execution hashes are both `836686f0fc5dc51a8ecca3260d316ca37ea87ba6e26b501c0f404f4dd433b9dd`; raw-node-ID-set fingerprint `55ab491f6fdb810195aa009dc3d6d70b8aca1d1d68dcb6940c028b5075c1b640`. Coverage **93.59%**. Archive SHA-256 for the raw JUnit (deleted rather than retaining possibly sensitive properties): `22c031839be63ba633ce97f9ee0244fbba887bd0a776ebfcb346aff04ad6449e`. All 126/126 teardown guards enabled at start and finish; zero other client backends at cleanup.
* Publication: ordinary (non-forced) `git push origin 76376101614f209d9d7b4ab4a74ac87fdca4f832:refs/heads/ulw/cpi-h-kek-binding` exit 0. `git ls-remote --exit-code --heads origin refs/heads/ulw/cpi-h-kek-binding` returned `76376101614f209d9d7b4ab4a74ac87fdca4f832` at `2026-11-24T19:55:11+00:00` and again after removing the worktree.
* Cleanup: both ID/label/mount-checked containers, the named PG volume and Redis anonymous volume removed; exact name, label and volume absence verified. No unrelated containers, settings or ports touched. Local loopback ports 33294 and 33295 each rebound and released. Disposable `fsync=off` ended by **2026-11-24T19:55:14+00:00**. Local scratch, temporary executables, venv/caches and the lane worktree were absent by **2026-11-24T19:57:30+00:00**. The code branch and this redacted evidence are retained intentionally.
* `KEK-BINDING-RECEIPTS.json` contains the final full-unit status, historical JUnit counts/hashes (including interrupted/failed attempts), independent reference comparison, effective SQL ACLs, committed-tree identifiers and closure receipts. This was an exact-commit synthetic non-browser gate, not a browser, live-data, external audit, or hosted CI approval.

Frozen-plan SHA-256 (untouched): `1cb6d1702dc8960b7bccced4c69a0193418d22d29a521498b083eb6c891dff19`.

## Verification unit exit inventory

Earlier transient units used `--collect`; where a numeric success status was no longer cached, its basis is stated rather than inventing a systemd receipt. The final unit was explicitly held through exit. `—` means no JUnit was generated (not a substitute XML).

| Unit | Exit | JUnit | Basis |
|---|---:|---|---|
| `cpi-kek-candidate.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-collect-exact.service` | 0 | — | waited runner/command receipt |
| `cpi-kek-deps.service` | 0 | — | waited runner/command receipt |
| `cpi-kek-final-gate-76376101.service` | 0 | recheck.xml, final-gate-76376101.xml | held systemd ExecMainStatus/Result |
| `cpi-kek-full-76376101.service` | 2 | full.xml | systemd journal main EXIT_STATUS |
| `cpi-kek-gate-76376101.service` | 1 | gate-76376101.xml | systemd journal main EXIT_STATUS |
| `cpi-kek-isolation-76376101.service` | 1 | isolation.xml | systemd journal main EXIT_STATUS |
| `cpi-kek-isolation-r2.service` | 0 | isolation-r2.xml | manager journal (no failed main exit) and completed output |
| `cpi-kek-mutation.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-posture-target.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-precheck.service` | 1 | — | systemd journal main EXIT_STATUS |
| `cpi-kek-provision.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-server-log-proof.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-session-privilege-r2.service` | 0 | — | waited runner/command receipt |
| `cpi-kek-session-privilege.service` | 1 | — | systemd journal main EXIT_STATUS |
| `cpi-kek-static-76376101.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-suite-76376101.service` | 1 | suite-76376101.xml | systemd journal main EXIT_STATUS |
| `cpi-kek-target.service` | 0 | — | waited runner/command receipt |
| `cpi-kek-publish-audit.service` | 0 | — | final publication seal and completed manager journal, no failed main exit; the unit wrote the other receipts before its own exit |

The precommit `precheck` exit 1 was Ruff E501 on a test assertion (89 columns
for an 88-column limit), fixed before this SHA. The invalid preliminary suite
also observed a duplicate synthetic principal during setup after an interrupted
run; its database was rebuilt, not reused for certification. Neither failure
was suppressed or included as a pass.

**Independent post-cleanup readback:** the remote branch still pointed to the
full SHA above after the publisher exited. `systemctl --user list-units --all
'cpi-kek-*'` and the lane container/volume checks were empty, and neither
private scratch root nor the lane worktree existed. The publisher's terminal
journal seal for `KEK-BINDING-RECEIPTS.json` matched its independently computed
SHA-256: `825407de46490109d368aac8fab495f7a8423158699821539755d89723a60d63`.
