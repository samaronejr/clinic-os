# Service level objectives and the performance harness

This page defines the service level indicators (SLIs) behind the proposed
targets of the performance profile (plan section 7, PF) and records how the
local harness (`ops/perf`) measures them. Every target here is a **proposal**.
The harness produces L1 evidence: synthetic tenants on a throwaway database on
one workstation. No result below is a production SLO, and none shows a target
met in production.

## Profiles

| Profile | Shape |
|---------|-------|
| P-small | 10 clinics, small patient base; smoke profile |
| P-100 | 100 clinics x 10 clinicians x 25 visits/day = 25,000 visits/day |
| P-1000 | 1,000 clinics; definition only, needs a multi-node rig (the runner refuses it) |
| P-large-tenant | one tenant with 100,000 patients, 50 staff sessions, bulk import running |

## SLIs and SLO proposals

The metric names are the ones `/internal/metrics` exposes (todo 11,
`apps/core/telemetry.py`). Where no server-side metric can answer the target
the row says so: those targets are measured by the harness only and are a gap
to close before any production claim.

| SLO | Target (proposal) | Scenario | SLI |
|-----|-------------------|----------|-----|
| SLO-1 | navigation p95 <= 1.5 s | `staff_navigation` | `histogram_quantile(0.95, sum by (le) (rate(clinic_http_request_duration_seconds_bucket{route="workspace-home"}[5m])))` |
| SLO-3 | exact-name blind-index palette p95 <= 700 ms at 100,000 patients (ADR-015 trigger) | `patient_search_blind_index` | no dedicated route metric; harness only |
| SLO-4 | agenda p95 <= 700 ms server | `agenda_interaction` | `histogram_quantile(0.95, sum by (le) (rate(clinic_http_request_duration_seconds_bucket{route="agenda-board"}[5m])))` |
| SLO-5 | autosave ack p95 <= 1 s | `autosave` | `histogram_quantile(0.95, sum by (le) (rate(clinic_http_request_duration_seconds_bucket{route="ui_api:ehr-autosave"}[5m])))` |
| SLO-6 | realtime p95 <= 2 s after commit | `realtime_fanout` | no metric: delivery is not instrumented; harness only |
| SLO-7 | transcript partial p95 <= 3 s | `transcript_partial` | no metric: modeled by the harness, not measurable until ASR is live |
| SLO-8 | final draft p95 <= 30 s after stop | `draft_generation` | `rate(clinic_ai_invocation_latency_seconds_sum{purpose="scribe_draft"}[5m]) / rate(clinic_ai_invocations_total{purpose="scribe_draft"}[5m])` (mean only: p95 needs a histogram) |
| SLO-9 | availability 99.95% monthly (core requests) | not measured | `1 - sum(rate(clinic_http_request_duration_seconds_count{status_class="5xx"}[30d])) / sum(rate(clinic_http_request_duration_seconds_count[30d]))` |

On 2026-11-12 the Clinic Ops owner, through Root, selected **Exact only**:
the PF patient-search target at 100,000 patients covers the exact-name
blind-index palette. This decision resolves the scope ambiguity documented in
`.omo/evidence/clinic-ops-premium-intelligence/task-71/Search-options.md`.
It removes the earlier SLO-2 assignment to the independent substring registry;
it changes neither the search implementation nor the protected-data contract.

The legacy legal/social-name substring registry remains a separately measured
workflow with **no SLO assigned**. Every report retains its p50/p95/p99, sample
count and errors as `patient_search_registry`, with `target: null` and an
informational verdict, never a passing target. Its current route SLI is
`histogram_quantile(0.95, sum by (le) (rate(clinic_http_request_duration_seconds_bucket{route="patient-list"}[5m])))`.
Follow-up: the substring registry needs its own owner-approved performance
contract, including query semantics and scale, before any target is assigned.
The earlier 61,784.970 ms p95 remains visible in todo 71's L1 evidence; it is
not represented as passing.

Provider impact is a separate SLI: `avg_over_time(clinic_provider_health[30d])`
per capability, so a provider outage never counts against core availability.
A 99.95% monthly target leaves an error budget of 21.6 minutes in a 30-day
month (43,200 minutes x 0.0005).

Queue and outbox health use `clinic_queue_depth`, `clinic_queue_oldest_age_seconds`,
`clinic_outbox_operations` and `clinic_outbox_oldest_age_seconds`; the harness
fairness checks assert behaviour, these alert on it.

## Not measured by load generation

- Availability: needs a production monthly window.
- RPO <= 5 min and RTO <= 60 min including objects and keys: measured by the
  restore drill of todo 72.
- No acknowledged write lost: asserted by the autosave and restore tests.

## Scenarios and how honest each number is

`measured` runs the application code as `clinic_app` on real PostgreSQL and
Redis. `chunk_upload` measures the real `ingest_chunk` acknowledgement through
tenant commit: recording and consent authority, encryption, the durable
AudioChunk, reference-only outbox operation and receipt audit. Consent and
recording setup and pause/resume controls are outside the timed samples; each
sample uses a fresh server-issued capture epoch without changing clocks.
Asynchronous object-storage delivery is outside this acknowledgement boundary.
Historical reports that label chunk upload `component` measured sealing alone
and do not satisfy the ingestion measurement.
`modeled` is seeded arithmetic over assumed provider timings (`transcript_partial`,
`draft_generation`); it is never counted as a target met and reports as
informational. HTTP scenarios (`staff_navigation`, `agenda_interaction`) are
server-side time through the full middleware stack with a real password + TOTP
sign-in, with no browser and no network. They run in four worker processes (one
GIL each, as gunicorn workers would) in a closed loop. Autosave editors are
threads paced to 12 saves per second overall. Each simulated editor retains its
session across repeated trials, and any failed save invalidates a fairness run.
The fairness load runs in
separate processes, so the measured path never queues behind the load
generator's own Python.

### Owner decision: production-like resource rig, 2026-11-13

The Clinic Ops owner, through Root on 2026-11-13, selected a production-like
resource rig for the canonical public command. PF (plan lines 336-337) says
"clinical queue isolated" and specifies no numeric split. The application
parent remains fixed at **2 CPUs and 4 GiB**, with no swap, split into:

- clinical/API: **1 CPU and 3 GiB**, including HTTP workers and autosave editors;
- AI-admission/bulk workers: **1 CPU and 1 GiB**;
- external flood producers: **2 CPUs and 1 GiB**, in a sibling cgroup
  outside the application parent, with CPU accounted separately.

The equal CPU split reserves clinical capacity under background saturation.
Clinical memory receives 3 GiB for the large patient base and concurrent
editors; admission workers receive 1 GiB for their bounded service processes.
The two unbounded external producers perform transport and bookkeeping only.
Private synchronous connections send each attempt to real admission code in
the AI-worker share; producer processes never execute `fair_acquire`. The
victim's admission checks also run in that worker share. PostgreSQL and Redis
remain shared and unchanged. Cgroups are verified from kernel controls and
unavailable isolation refuses the public run; it never falls back to the
rejected shared topology.

This resolves a fidelity problem in the previous rig: external producer
bookkeeping, admission and clinical execution all competed inside one shared
2-CPU budget. That result remains **rejected**, not passing: shared R2 median
69.386 ms baseline / 77.993 ms loaded, ratio **1.124**. It completed 2,053,943
offered attempts, 1,577 admitted and 2,052,366 deferred; coalescing reduced
quota SQL from 456,523 to 292,412 without meeting the latency guard.

The public command below uses the new rig by default. It retains two unbounded
producers, real authority and audit, 12 editors and 300 saves on
P-large-tenant, five alternating trials, all scenarios and the **1.10**
fairness limit. Each trial reports offered/admitted/deferred counts and
clinical, worker and external producer CPU separately. A lower offered load
does not demonstrate isolation; every acceptance report must show these
counts alongside the prior shared result and explain the declared budgets.
Every measured save window must contain both actual admitted work and
deferrals; cumulative warm-up or post-save counters cannot satisfy that check.
This decision changes the rig, not product quotas, publication semantics,
audit durability, traffic pacing, targets or the scope of L1 synthetic claims.
The decision and rejected measurements are retained in todo 71 evidence at
`.omo/evidence/clinic-ops-premium-intelligence/task-71/RIG-OWNER-DECISION-20261113.md`.

## Fairness

Per-tenant token buckets protect the clinical queue (`apps/core/fairness.py`).
The harness checks two things on the focus tenant, as the median p95 of five
alternating baseline/loaded trials after a discarded warm-up, passing when the
loaded p95 stays within 10% of the baseline:

- `chart_save_during_bulk_import`: autosave p95 while a governed import writes
  to the same tenant. Each trial uploads a 10,000-patient synthetic CSV, maps
  it, runs validation, approves the cutover and queues immutable batches using
  the real verified operator session. One load process directly invokes
  `apps.interop.tasks.commit_batch`; it runs the actual task body rather than
  a separate Celery worker. The task performs its actual quota and
  authority checks and owns each committed tenant transaction. Patient
  registrations prepare their derived rows before their ordered audits and
  commit individually, so one patient cannot retain the organization chain
  for the next patient. Bulk work rolls back and defers rather than queueing
  at a busy audit chain. Every trial must commit additional rows during the
  measured saves; a warm-up commit alone cannot satisfy the workload check.
  Exhausting the source
  before the clinical trial finishes invalidates the load.
- `clinical_latency_during_ai_batch_flood`: a second tenant floods `ai-batch`.
  It also requires the flooder to be deferred and the victim tenant to be
  admitted on `ai-batch` every time.

## Running it

Use a private throwaway PostgreSQL (never port 5432) bootstrapped with
`make db-bootstrap` and migrated with `make migrate`, and a Redis for the
broker and realtime. Provide `MIGRATION_DATABASE_URL`, `APP_DATABASE_URL`,
`CELERY_BROKER_URL`, `REALTIME_ENABLED=true` and `REALTIME_REDIS_URL`.
The local executor must provide a delegated Linux cgroup-v2 user systemd
manager with CPU and memory controllers and `busctl`/`systemctl`. The runner
also requires its owned launcher/return unit to have
`Delegate=cpu memory pids`; it checks that before moving any process.
The executor grants that property, without changing the application budgets.
Systemd 255 refuses `AttachProcessesToUnit` for a non-delegated return target.
Systemd command failures retain their actual stderr instead of being labeled
as missing CPU/memory capability. Teardown closes the controller pipe even if
restoration fails, so failed application scopes cannot strand the controller.
The heavy-lock executor uses `flock --close`: only its supervisor retains the
lock descriptor, including when business processes move to another scope.
The runner
creates invocation-owned groups, restores its original process placement,
then removes only those groups. An idle control process keeps the original
launcher scope alive until restoration, including single-process launchers.
The invocation groups bind to that launcher so cancellation stops its relocated
workers and producers. If the driver disappears while its terminal or launcher
remains alive, the idle controller stops only that invocation's groups.
Failed startup, nonzero child exits, forced termination
or a missing victim response invalidate the run rather than count as evidence.

```bash
systemd-run --user --scope -p "Delegate=cpu memory pids" \
  uv run python -m ops.perf.run --profile P-large-tenant \
    --run-root /absolute/private/task-owned-run-root
```

This standard executor wrapper supplies the required return context; the
public entrypoint remains `python -m ops.perf.run` and always uses the declared
production-like rig. A private diagnostic is not acceptance.
The explicit run root must already exist, be executor-owned with mode 0700,
and have no symlink components. The `runtime_directory` context owns the
synthetic KEK/media child and removes it on successful runs and setup failures.
Do not run canonical performance measurements without the owner's quiet window.

The report is JSON validated against `ops/perf/report-schema.json` and carries
the profile, dataset, commit sha, per-scenario p50/p95/p99 and pass/fail against
the target, the fairness verdicts, the ADR-015 search benchmark and the targets
that load generation cannot observe. The exit code is non-zero when any target
fails, so a regression is caught. `--inject-slow-query-ms` delays every patient
patient lookup statement inside the database to prove a slow query is recorded
as a failure.
The Locust file `ops/perf/locustfile.py` drives the same pages over HTTP
against a locally running synthetic server.
It obtains fresh CSRF tokens for password and required TOTP submission and verifies
the protected authenticated route before browsing. Authentication, CSRF or
expired-session failures mark the request failed, stop the load and force
exit status 1. One configured account/authenticator supports one virtual user;
concurrent users must not replay a shared TOTP code.

## ADR-015 search benchmark

ADR-015's revisit trigger is a measured p95 miss on blind-index patient search
in this harness. The report's `search_benchmark` records the blind-index p95,
the substring registry p95 and the verdict. The verdict is
`not_evaluated_dataset_below_100000_patients` on any profile whose focus tenant
is smaller than 100,000 patients, so only P-large-tenant can trigger it.
