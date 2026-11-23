# Todo 83 Option C — unmerged agenda headroom lane

**Implementation:** `c2186436bcbddd02c8ed1f2c35400d0329c0840c` on `ulw/cpi-h-t83c`, based on `origin/ulw/cpi-wave-h` at `4f8d2454987ca1d083ead53a7501126d647a89bc`. This claim is a subsequent evidence-only commit on that lane. Nothing is merged. Option C only addresses agenda cost; it does not establish either unchanged F4/N1 gate or close todo 83. Both gates (agenda p95 ≤700 ms, AI-flood ratio ≤1.10) still need to pass at the integrated final head and on the prespecified repeat.

## 1. Attribution before changing code

**What F4 retained.** Paths to earlier evidence refer to the integration checkout, not copies in this lane. `../final-wave/head-13a6eae8594d/F4-ATTRIBUTION.md` §§1–2 and its `F4-run/P-large-tenant/perf.json:358–373` identify the pooled 300-GET, 300-booking, 50-receptionist, four-process day/week/list scenario, p95 **701.665 ms**. Its `F4-run/P-large-tenant.log:129–888` contains 372 board HTTP completions including staff navigation; it has duration/method/status/route, not the view, SQL, binds, or per-query durations. The same fields survive in `../final-wave/head-93aa8a818228/F4-run-attempt-2/` (p95 **692.277 ms**). Neither log supports extracting an individual F4 SQL trace or identifying the view at p95. `../task-81/REVIEW.md:89–102` reconciles the small live GET fixed costs, and `../task-82/before-corrected/measurement.json:1–560` records 130/130/129 statements (75 decrypt executions each) for the 75-booking serial diagnostic. `../final-wave/head-93aa8a818228/T83-HEADROOM-OPTIONS.md` §§2, 3C separate this endpoint's work from the timed fairness stage.

**Request equivalence.** `ops/perf/run.py:173–191` runs the board scenario before fairness. `ops/perf/scenarios.py:188–202` uses the full `/scheduling/clinics/<clinic>/agenda/board/{day,week,list}/2035-06-04/` GET, no HX, filters, or patient pin. `ops/perf/staff_http.py:31–55,85–108,125–137` provisions a one-clinic receptionist, posts the real password and verify flow, then `Client.get(..., follow=True)` as `clinic_app`. Its verify view redirects a nonprivileged receptionist without binding a device (`apps/identity/otp_views.py:128–146`); this explains the absence of a separate device-table read. `ops/perf/profiles.py:85–96` and `ops/perf/dataset.py:123–193,406–450` imply 12 active practitioners × 25 appointments with distinct booked patient rows, all on one clinic day. They all fit the 400-row display bound. Week layout renders one subject lane but the existing unfiltered authorized board DTO and its signature use the same 300 appointment rows (`apps/scheduling/agenda_board.py:398–421,545–585`; `apps/scheduling/agenda_board_presenter.py:234–293,352–360`, base lines).

I reproduced **the GET statement shape**, not F4 timing: one isolated PG16 container, an exact synthetic 12×25 booked day using the same seed helpers, one real signed-in receptionist with matching role/tenant/runtime context, 300 patient rows rather than the 100,000-patient registry, no second tenant/load workers, no benchmark loop. Executed one GET per view on the remembered-clinic session on each revision, with its first day GET captured separately. Only SQL shapes, repetition counts, response status and statement totals left process memory; no SQL values, keys, credentials, or response bodies were retained.

### Warm day statement census, in execution order (week same count; list omits occupancy)

Django-observed executions and transaction controls. Each cleanup execute packs five top-level SQL commands; the count does not include statements inside the server's resolvers/triggers or connection/fixture setup. `B` line numbers are at Wave H base; `C` line numbers are at the implementation SHA. An unqualified source line is unchanged.

| B ordinal | C ordinal | Purpose | Statement shape and issuer |
|---:|---:|---|---|
| 1 | 1 | Session | Load unexpired `django_session` by cookie key (`apps/tenancy/middleware.py:168–170`; Django `contrib/sessions/backends/db.py:32–43`). |
| 2–4 | 2–4 | Tenant hygiene | `BEGIN`; four fixed `RESET app.current_*` commands + one `SELECT set_config(...)` packed in one execute; `COMMIT` (`apps/tenancy/middleware.py:174`; `apps/tenancy/db.py:35–52`). |
| 5 | 5 | Session | `SELECT clinic_app.identity_session_active(user, cookie_hash)` (`apps/identity/session_inventory.py:124–132`). |
| 6–9 | 6–9 | Tenant authorization/bind | `BEGIN`; `set_config(app.current_user_id, …, true)` guarded by runtime SQL role; `user_has_org(…)`; `set_config(app.current_tenant, …, true)` (`apps/tenancy/db.py:56–89`). |
| 10–13 | 10–13 | Route/role | Route's `unnest(permissions) WHERE has_permission(candidate, clinic, NULL)`; signed-user `load_current_user()`; two role `EXISTS` queries (physician, privileged-admin role set) (`apps/core/workspace_access.py:56–72`, `apps/core/navigation.py:517–532`, `apps/identity/auth_backends.py:58–75`, `apps/identity/otp.py:213–215,267–276`, `apps/identity/models.py:42–61`). |
| 14 | 14 | Board control | `SAVEPOINT` (`apps/scheduling/agenda_board.py:B527/C529`). |
| 15–18 | 15–18 | Board role/scope | Guarded actor GUC read → active user resolver → `SELECT role` for actor and exact clinic → `SELECT Clinic` for exact clinic (`apps/scheduling/access.py:65–99`; `apps/identity/current_context.py:54–98`). The legacy receptionist check passes, so no fallback role branch. |
| 19 | 19 | Appointments | `SELECT DISTINCT practitioner_id` over scoped, in-window, non-due board bookings (`apps/scheduling/agenda_board.py:B537–546/C539–548`). |
| 20–22 | 20–22 | Availability/catalog | `scheduling_service_practitioners(clinic)`; ordered active rooms/equipment; ordered active service types (`apps/scheduling/agenda_grid.py:156–166`, `apps/scheduling/resource_booking.py:82–88`, `apps/scheduling/agenda_board.py:B303–322/C304–323`). No separate `AvailabilityBlock` read on this GET; the free-slots and mutation paths are different actions. |
| 23–25 | 23–25 | Move legacy role | Guarded actor GUC → user resolver → exact-clinic manager `EXISTS` (`apps/scheduling/agenda_grid.py:177–189`, `apps/identity/current_context.py:54–98,110–125`). |
| 26–28 | 26–28 | Move service permission | Guarded actor GUC → user resolver → `has_permission(appointment.move, …)` (`apps/scheduling/agenda_grid.py:169–189`, `apps/identity/current_context.py:172–201`). |
| 29–31 | 29–31 | Move own permission | Guarded actor GUC → user resolver → `has_permission(appointment.move_own, …)` (same lines). |
| 32–34 | 32–34 | Book permission | Guarded actor GUC → user resolver → `has_permission(appointment.book, …)`; the receptionist's general grant short-circuits `book_own` (`apps/scheduling/agenda_board.py:B333–338/C334–339`, `apps/identity/current_context.py:172–201`). |
| 35 | 35 | Appointments/patients | One ordered, limited appointment SELECT with **inner** patient join, indexed FK equality, FORCE RLS on both sides. Before: appointment and joined patient fields except birth date. After: only needed appointment columns plus `patient.full_name` as binary ciphertext (`apps/scheduling/agenda_board.py:B537–570/C539–587`, `apps/scheduling/models.py:229–230`). |
| 36–335 | 36 | Patient name conversion | One `SELECT clinic_app.protected_decrypt(KEK, purpose, envelope)` per non-null name at baseline (`apps/intake/models.py:83`, `apps/tenancy/fields.py:98–109,128–132`, `apps/tenancy/envelope.py:B117–127,210–219`). One ordered `unnest(bytea[]) WITH ORDINALITY` and same `protected_decrypt` for each non-null element after (`apps/scheduling/agenda_board.py:C588–607`, `apps/tenancy/envelope.py:C223–262`). |
| 336 | 37 | Appointments: occupancy | Time/lane/clinic-only interval SELECT for day/week; list has **0** (`apps/scheduling/agenda_board.py:B424–448,587–594/C425–449,608–615`). No patient join on this path. |
| 337–338 | 38–39 | Audit | Tenant/actor GUC read; `audit_append(…)`, one `scheduling.agenda.viewed` for the clinic (`apps/scheduling/agenda_board.py:B621–625/C642–646`; `apps/audit/services.py:114–151,197–223`). |
| 339 | 40 | Board control | `RELEASE SAVEPOINT` (same board atomic block). |
| 340 | 41 | Presentation | Active saved views for current user/clinic under RLS (`apps/scheduling/agenda_board_views.py:256–268`, `apps/identity/saved_views.py:81–86`). |
| 341–343 | 42–44 | Shell authority | Actor's role assignments joined to clinic → full registry `has_permission` for discovery → same registry for a fresh selected-clinic decision (`templates/base.html:4`, `apps/core/workspace.py:148–182,250–298`, `apps/core/navigation.py:517–532`). |
| 344–346 | 45–47 | Shell patient scope | Guarded actor GUC → user resolver → `has_permission(demographics.read, …)`. No enrollment or demographic read/audit: no patient binding/pin (`apps/core/patient_context.py:200–213,312–345`, `apps/identity/current_context.py:172–201`). |
| 347–348 | 48–49 | Presentation | User theme/density; latest clinic branding configuration (`templates/base.html:5,84–95`, `apps/identity/preferences.py:58–79`, `apps/identity/clinic_configuration.py:121–127`). |
| 349–351 | 50–52 | Session touch | Guarded actor GUC → user resolver → one live-session `UPDATE` with `FOR NO KEY UPDATE SKIP LOCKED` in its subquery (`apps/tenancy/middleware.py:227–245`, `apps/identity/session_inventory.py:312–326`). |
| 352–355 | 53–56 | Tenant hygiene | Durable tenant `COMMIT`; cleanup `BEGIN`, reset batch, `COMMIT` (`apps/tenancy/db.py:35–52,63–91`). |
| — | — | Waitlist | **0**; no offer-list or reconciliation service on the board GET. |

**Repeated SQL and rationale:** exact guarded actor-GUC SQL ×7 and `load_current_user()` SQL ×8 across separate checks; exact same full permission registry SQL and args ×2 (discovery versus later shell, *not* the narrowed route registry); identical resets ×2 and `BEGIN`/`COMMIT` ×3 each, at independent lifecycle boundaries. None is a redundant authorization result under READ COMMITTED (`apps/identity/current_context.py:178–182`, `apps/core/workspace.py:267–268`). Book/move use equal statement templates with different permission binds. The actual N+1 is the **joined encrypted field conversion**: 300 same-shape decrypt statements with different envelope inputs, no N patient-table SELECTs. Batching is justified; caching authorization or skipping the banner check is not.

| Observed get/role shape | Before | After | Removed executions |
|---|---:|---:|---:|
| First day GET after sign-in | 358 | 59 | 299 |
| Warm day | 355 | 56 | 299 |
| Warm week | 355 | 56 | 299 |
| Warm list | 354 | 55 | 299 |

All requests returned 200 with no redirects. The first day GET additionally persisted remembered-clinic state (`apps/core/workspace.py:185–222`, Django DB session save+BEGIN/COMMIT after the response); warmed results had `session.modified=False`. Each pair of reset batches accounts for an additional 8 top-level server commands if expanded; the −299 delta is the same. The unchanged `protected_decrypt` does **not** run 299 fewer times in PostgreSQL: the app makes one call over the array and the function still checks/decrypts each of the 300 names.

## 2. Diff and invariant review

Seven code/test paths, **+247/−9**: the board name projection and `reveal_many` helper; populated board-budget/truncation/denial tests; protected-field integration tests; one observed resolver row and executable probe in the authority census; two `server-state` serial classifications for the new scheduling-world node IDs. No templates, migrations, grants, settings, fairness/lock/touch code, perf code, frozen plan, or required-browser list changed.

- **Same authority order and row scope.** Middleware/route/privilege checks, `authorized_agenda_scope`, catalog/filter validation, and all move/book checks still precede the same joined row read. `ExpressionWrapper(..., BinaryField())` only changes the selected `bytea` value's Django converter; the nonnullable patient FK keeps the inner join. Organization, clinic, date, status, due-hold, own-practitioner and selected filters are unchanged. No broad patient prefetch, new database function, SQL privilege or bypass. Key scope is still checked per non-null member by `clinic_app.protected_decrypt` (`apps/tenancy/migrations/0004_protected_fields.py:85–138,171–199`), not taken from an input tenant or same-request grant snapshot. Public services retain keyword-only explicit clinic scope.
- **Same output and safe bounds.** The sorted tuple uses the same plaintext-independent `start_at, practitioner_id, pk` order; ordinality restores array input order, not name order. The `MAX_BOARD_APPOINTMENTS+1` row affects truncation only and is never decrypted. Nonmatching, wrong-clinic and anonymous occupancy rows never enter the batch; month remains counts-only. NULL → empty display string and non-null→UTF-8 match the old field codec. No birthday, new key material, new plaintext destination, call-site audit data, or cross-request cache; only values needed by `_presented` are loaded. `reveal_many` re-fetches the KEK for each nonempty non-null call, never exposes the DEK, uses one parameterized statement, rejects bad lengths/types, and maps the original DB failure classes without yielding a partial batch. The batch shares one statement snapshot and one KEK read; each envelope still selects its own DEK version through the unchanged resolver. This sharing does not include any request grant check. Rejected a plaintext SQL predicate, a grant cache, and batching permissions/availability (separate fresh authority decisions or already set-based).
- **Denial, audit, and transaction/touch parity.** Foreign, same-tenant wrong clinic and unknown selectors are still refused before joined data, decrypt, or audit. Existing independent view 404 remains unchanged. Audit still appends exactly once at the original point after assembly inside the board savepoint; an error yields no render and no audit for that board. Event/chain/id semantics are unchanged; this event's actual payload remains `clinic_id, object_verb` (`apps/audit/events.py:1296–1301,1422–1451`), within root allowlist `clinic_id, http_method, http_status, object_verb, reason_code, request_id`. Audit/denial checks, `SKIP LOCKED` touch, tenant GUC resets and fair admission code are unmodified.

## 3. QA and headroom projection

| QA | Result |
|---|---|
| Happy | Full signed-in day/week/list renders distinct synthetic names in order with one decrypt statement and live guards/touch, pinned at 56/56/55; 300-name replay agrees at those budgets. Truncated and month cases retain expected counts/names. **No P-large latency or fairness acceptance run.** |
| Failure | Temporary test-process-only hook added one `SELECT 1` ahead of the real `_read_board` per GET. Three tests red (exit 1), all at the exact count assertion, after HTTP 200: 57 != 56, 57 != 56, 56 != 55. Unhooked, same three pass (exit 0). |
| Adversarial | **105 passed**, real PostgreSQL, including owner and physician scope, lane visibility, wrong/unknown clinic denials, byte-normalized refusal parity, no audit/touch on denial, audit-on-success, FORCE RLS/ACLs, stale/narrowed authorization, eight staff-boundary GUC tests, and nonblocking touch/no touch of a revoked row. New crypto tests reject wrong tenant, purpose, context, malformed mixed batch, changed/missing KEK; preserve order, nulls and empty sets. **7 passed** against real Redis/PostgreSQL for tenant quota isolation, concurrent quota, immediate increases/decreases, skew/expiry parity and outage ordering. Static inventory and live observer for the new authority row passed, **975** identified Python candidates. Broad all-symbol executable authority replay did not complete (480 s budget), not a pass. |

From the prior T82 75-booking instrumented diagnostic, component-only summed driver `execute` time per request (`../task-82/{before-corrected,after}/measurement.json`, agenda per-view `instrumented_pg_execute_ms.decrypt.sum / 6`):

| View | Scalar baseline | Batched WIP | Net driver delta | Baseline component / separate pre-WIP mean wall time |
|---|---:|---:|---:|---:|
| Day | 28.615 ms | 20.080 ms | 8.534 ms | ~37% |
| Week | 29.581 ms | 19.827 ms | 9.754 ms | ~39% |
| List | 29.662 ms | 19.771 ms | 9.891 ms | ~39% |

These means are a component comparison from six instrumented requests per view, and the wall-time denominator is the separate 24-request serial sample. The earlier WIP also touched render/banner; only the decrypt execute bucket is used, not its combined end-to-end improvement. No 300-name SQL timing was retained at F4 and no timing was solicited from this lane's census. **Projection, not a measurement:** if the net saved driver time scales roughly 4× from 74 to 299 removed round trips and transfers to the same tail requests (large assumptions), it suggests **~34–40 ms** direct headroom: roughly **652–658 ms** from 692.277, or **662–668 ms** from 701.665. Backend cost still scales with names; planner, batch I/O and contention/rendering may flatten or amplify that. The evidence does **not** establish ≤630 ms or even a repeatable pass at 700 ms; there is no expected direct AI-flood ratio change.

## 4. Executed commands and cleanup receipt

Commands below use Bash argv notation (`"${N[@]}"` = `nice -n 10 ionice -c 3`; `"${U[@]}"` = `uv run --frozen --no-sync --no-env-file`; `"${P[@]}"` = `"${U[@]}" pytest --reuse-db -q --tb=short`). All were awaited in process; long tests had completion watches. No sleeps added. `APP_DATABASE_URL`, `MIGRATION_DATABASE_URL`, `TEST_SUPERUSER_DATABASE_URL`, `AGENT_DATABASE_URL`, PG and role passwords were child environment variables only. No DSNs/credentials in argv or this receipt.

- **Setup (all 0):** `git rev-parse origin/ulw/cpi-wave-h`; `git worktree add -b ulw/cpi-h-t83c /home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-h-t83c origin/ulw/cpi-wave-h`; `uv sync --locked --all-groups --offline`. All following invocations use that worktree. `PG=cpi-t83c-pg-st01a205a3`; container port `55443`, assigned host port **32885** on `127.0.0.1`; task directory `S=/run/user/1000/cpi-t83c-st01a205a3-D0AqAt`.
- **PG (all 0 except the intentionally ended log follower):** `"${N[@]}" docker run --pull=never -d --name "$PG" --cpus 1 --memory 768m --memory-swap 768m --pids-limit 128 --shm-size 128m --tmpfs /var/lib/postgresql/data:rw,nosuid,noexec,size=512m -p 127.0.0.1::55443 -e PGPORT -e POSTGRES_USER -e POSTGRES_PASSWORD -e POSTGRES_DB --entrypoint /usr/bin/ionice postgres:16.14 -c 3 nice -n 10 docker-entrypoint.sh postgres -p 55443 -c shared_buffers=96MB`; `docker logs --follow "$PG"` (observed the final server-ready event, terminated/reaped follower); `docker port "$PG" 55443/tcp`; `"${N[@]}" make db-bootstrap`; `"${N[@]}" "${U[@]}" python manage.py migrate --noinput --settings=config.settings.test` for `clinic` and `test_clinic`, changing only the owner DSN via environment; `"${N[@]}" "${U[@]}" python manage.py makemigrations --check --dry-run --settings=config.settings.test` (no changes).
- **Census:** `OPTION_C_ROOT="$S" "${N[@]}" "${U[@]}" python -c "$CENSUS"` on base and candidate (0/0); initial pre-run command had a Python string quoting syntax error (1, no DB access). The temporary census was not a perf profile. Data construction and body are given below.
- **Static:** `files=(apps/scheduling/agenda_board.py apps/tenancy/envelope.py tests/identity/nonstaff_infrastructure_probes.py tests/renewal/test_encryption_readiness.py tests/scheduling/test_agenda_board.py)`; `"${U[@]}" ruff check "${files[@]}"`; `"${U[@]}" ruff format --check "${files[@]}"`; `"${N[@]}" "${U[@]}" mypy --strict "${files[@]}"`; `git diff --check`; `git diff --cached --check` (each **0**). Initial check/format/type runs were all 1: two overlong test lines and an explicit-import mypy diagnostic, fixed before the recorded tests.
- **Mutation QA:** `BUDGET=tests/scheduling/test_agenda_board.py::test_full_agenda_board_query_budget`; `"${N[@]}" "${U[@]}" python -c "$INJECTION" --reuse-db -q --tb=short "$BUDGET"` (**1** expected); `"${N[@]}" "${P[@]}" "$BUDGET"` (**0**, 3 pass). See test-only hook below.
- **Owned Redis process (no second container):** `"${N[@]}" docker image save --platform linux/amd64 -o "$S/redis-image.tar" redis:7-alpine` (**0**); `"${N[@]}" "${U[@]}" python -c "$EXTRACT" "$S/redis-image.tar" "$S/redis-root"` (**0**, from the single manifest image's layers, copied only regular `usr/local/bin/redis-server`, `lib/ld-musl-x86_64.so.1`, `usr/lib/libcrypto.so.3`, `usr/lib/libssl.so.3`, preserving mode). `"${N[@]}" "$S/redis-root/lib/ld-musl-x86_64.so.1" --library-path "$S/redis-root/lib:$S/redis-root/usr/lib" "$S/redis-root/usr/local/bin/redis-server" --version` (**0**, 7.4.11). A mode-0700 `$S/redis-server` exec wrapper for that command passed `"$@"` instead of `--version`, so the unchanged `fairness_broker` fixture owned the event-driven, loopback-only child and reaped it.

Exact scoped DB and non-DB selections:

```bash
"${N[@]}" "${P[@]}" --show-capture=no \
  tests/scheduling/test_agenda_board.py tests/scheduling/test_agenda_grid.py \
  tests/scheduling/test_agenda_audit.py tests/scheduling/test_appointment_rls.py \
  tests/scheduling/test_scheduling_rls.py tests/core/test_workspace_agenda_board_refusals.py \
  tests/core/test_workspace_grant_freshness.py tests/renewal/test_encryption_readiness.py \
  tests/patients/test_intake_rls.py tests/identity/test_tenant_guc_boundaries.py \
  tests/auth/test_touch_lock_order.py::test_touch_skips_a_locked_row_and_never_touches_a_revoked_one
# 0: 105 passed; 186 requests, 82 refusals, 0 refusal guard violations

PATH="$S:$PATH" CLINIC_BROKER_GATE=required CELERY_BROKER_URL=memory:// \
  "${N[@]}" "${P[@]}" --show-capture=no \
  tests/core/test_fairness.py::test_redis_outage_defers_metered_queues_and_fails_clinical_open \
  tests/core/test_fairness.py::test_malformed_quota_still_denies_clinical_during_redis_outage \
  tests/core/test_fairness.py::test_exhausted_bucket_observes_published_quota_increase_immediately \
  tests/core/test_fairness.py::test_credited_bucket_observes_published_quota_decrease_immediately \
  tests/core/test_fairness.py::test_token_bucket_enforces_per_org_quota \
  tests/core/test_fairness.py::test_concurrent_acquires_never_exceed_quota \
  tests/core/test_fairness_notifications.py::test_bucket_decisions_match_across_idle_expiry_with_clock_skew
# 0: 7 passed, none skipped. Not the F4 broker gate.

"${U[@]}" pytest -q --tb=short tests/infra/test_module_boundaries.py \
  tests/infra/test_ci_pytest.py::test_serial_manifest_grammar_is_closed \
  tests/infra/test_ci_pytest.py::test_serial_entry_matches_node_children_and_parametrizations \
  tests/infra/test_ci_pytest.py::test_tracked_serial_classification_parses \
  tests/infra/test_ci_pytest.py::test_grid_refusal_module_stays_in_the_parallel_partition \
  tests/infra/test_ci_pytest.py::test_board_refusal_module_stays_in_the_parallel_partition \
  tests/infra/test_ci_pytest.py::test_shared_catalog_maintenance_is_serial
# 0: 281 passed (isolated DSN environment supplied defensively)
```

**Non-passes retained explicitly.** `"${N[@]}" "${P[@]}" --show-capture=no tests/identity/test_permission_parity.py::test_every_authorization_candidate_is_accounted_for` exceeded the 480 s eval budget and its child was stopped without a pytest exit receipt; no matching process remained (`pgrep` exit 1). It was not retried. `PYTHONPATH=tests OPTION_C_ROOT="$S" "${N[@]}" "${U[@]}" python -c "$PROBE"` checked exact `discover()`/manifest equality and executed the newly declared infrastructure probe using the production observer against the real database (**0**, GUC `app.current_tenant` observed; first driver-only attempt **1** because it asserted the resolver itself was an observer root). Separately, `"${U[@]}" pytest -q --tb=short tests/infra/test_module_boundaries.py tests/infra/test_ci_pytest.py` without that env was too broad (**1**: 307 passed, 4 missing-superuser-env failures, one rejected loopback default-port login). The port-5432 handshake was accidental; authentication refused before queries could execute. No service was created or changed there. The exact pure selection above replaced it; all successful DB tests used the owned port.

**Cleanup:** `docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$PG"` (0, no volumes; storage tmpfs). `"${N[@]}" docker rm -f -v "$PG"` (0), `docker ps -a --filter "name=^/$PG$" --format '{{.Names}}'` (0, no row), `ss -H -ltn sport = :32885` (0, no listener). No test or Redis wrapper/loader process remained; task runtime directory including exported Redis archive and test hook receipts removed and verified absent. No browsers, perf profiles, F4 rig, native gate, `make ci`, benchmark threshold edits, or clean-head acceptance claim.

### Temporary probes (kept out of product and CI)

The census ran this request body in `ops.perf.runtime.bootstrap_django(Path(os.environ["OPTION_C_ROOT"]))`, using the repository's real fixture methods. The inline classification grouped statement templates and used exact command equality in memory to report duplicate multiplicities only:

```python
tenant = seed_tenant(SimpleNamespace(clinicians_per_clinic=12), index=0)
seed_patients(tenant, 300, sample=300)
assert seed_agenda(tenant, visits_per_clinician=25) == 300
[login] = provision_logins(tenant, 1)
client = Client(HTTP_HOST="localhost")
with runtime_role():
    sign_in(client, login)
    for view in ("day", "day", "week", "list"):
        with CaptureQueriesContext(connection) as captured:
            response = client.get(
                f"/scheduling/clinics/{tenant.clinic_id}/agenda/board/{view}/{AGENDA_DAY}/",
                follow=True,
            )
        assert response.status_code == 200 and not response.redirect_chain
        sqls = [entry["sql"] for entry in captured]
        # Group by operation/function/table as in the census above; rows
        # bound for the same protected_decrypt template are not duplicates.
        print(view, len(sqls), sum("protected_decrypt(" in s for s in sqls))
```

`$INJECTION` payload:

```python
import sys
import pytest
from django.db import connection

class ExtraQuery:
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        from apps.scheduling import agenda_board_views
        original = agenda_board_views._read_board

        def injected(*args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return original(*args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(agenda_board_views, "_read_board", injected)
            return (yield)

raise SystemExit(pytest.main(sys.argv[1:], plugins=[ExtraQuery()]))
```

`$PROBE` used the same private synthetic bootstrap on `clinic`, asserted `discover() == {row["symbol"]: row["signals"] for row in INVENTORY["candidates"]}`, verified the new row's `#observed` declarations, seeded one empty clinic and encrypted a synthetic value there, then built the real `infrastructure_probes(...)` list with that envelope. It selected the `reveal_many` row and ran `_invoke(probe, actor, tenant.organization_id, authority=AuthorityObserver(catalog, catalog.derive()))`, required the expected true result, and required `("guc", "app.current_tenant")` in the observer touches. Only the count and pass outcome were printed.
