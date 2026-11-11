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
| SLO-2 | patient search p95 <= 700 ms server | `patient_search_registry` | `histogram_quantile(0.95, sum by (le) (rate(clinic_http_request_duration_seconds_bucket{route="patient-list"}[5m])))` |
| SLO-3 | patient search by blind index p95 <= 700 ms (ADR-015 trigger) | `patient_search_blind_index` | no dedicated route metric; harness only |
| SLO-4 | agenda p95 <= 700 ms server | `agenda_interaction` | `histogram_quantile(0.95, sum by (le) (rate(clinic_http_request_duration_seconds_bucket{route="agenda-board"}[5m])))` |
| SLO-5 | autosave ack p95 <= 1 s | `autosave` | `histogram_quantile(0.95, sum by (le) (rate(clinic_http_request_duration_seconds_bucket{route="ui_api:ehr-autosave"}[5m])))` |
| SLO-6 | realtime p95 <= 2 s after commit | `realtime_fanout` | no metric: delivery is not instrumented; harness only |
| SLO-7 | transcript partial p95 <= 3 s | `transcript_partial` | no metric: modeled by the harness, not measurable until ASR is live |
| SLO-8 | final draft p95 <= 30 s after stop | `draft_generation` | `rate(clinic_ai_invocation_latency_seconds_sum{purpose="scribe_draft"}[5m]) / rate(clinic_ai_invocations_total{purpose="scribe_draft"}[5m])` (mean only: p95 needs a histogram) |
| SLO-9 | availability 99.95% monthly (core requests) | not measured | `1 - sum(rate(clinic_http_request_duration_seconds_count{status_class="5xx"}[30d])) / sum(rate(clinic_http_request_duration_seconds_count[30d]))` |

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
Redis. `component` measures one primitive of a path whose full fixtures live in
another suite (`chunk_upload` times the audio sealing step, not `ingest_chunk`).
`modeled` is seeded arithmetic over assumed provider timings (`transcript_partial`,
`draft_generation`); it is never counted as a target met and reports as
informational. HTTP scenarios (`staff_navigation`, `agenda_interaction`) are
server-side time through the full middleware stack with a real password + TOTP
sign-in, with no browser and no network. They run in four worker processes (one
GIL each, as gunicorn workers would) in a closed loop. Autosave editors are
threads paced to 12 saves per second overall, and the fairness load runs in
separate processes, so the measured path never queues behind the load
generator's own Python.

## Fairness

Per-tenant token buckets protect the clinical queue (`apps/core/fairness.py`).
The harness checks two things on the focus tenant, as the median p95 of five
alternating baseline/loaded trials after a discarded warm-up, passing when the
loaded p95 stays within 10% of the baseline:

- `chart_save_during_bulk_import`: autosave p95 while import-shaped batches of
  200 encrypted registrations (the import's batch size) write to the same
  tenant, each batch admitted by the organization's `bulk` token bucket as the
  real bulk task does. The load is `create_patient` batches in one process (one
  bulk worker), not the interop job machinery.
- `clinical_latency_during_ai_batch_flood`: a second tenant floods `ai-batch`.
  It also requires the flooder to be deferred and the victim tenant to be
  admitted on `ai-batch` every time.

## Running it

Use a private throwaway PostgreSQL (never port 5432) bootstrapped with
`make db-bootstrap` and migrated with `make migrate`, and a Redis for the
broker and realtime. Provide `MIGRATION_DATABASE_URL`, `APP_DATABASE_URL`,
`CELERY_BROKER_URL`, `REALTIME_ENABLED=true` and `REALTIME_REDIS_URL`.

```bash
uv run python -m ops.perf.run --profile P-large-tenant
```

The report is JSON validated against `ops/perf/report-schema.json` and carries
the profile, dataset, commit sha, per-scenario p50/p95/p99 and pass/fail against
the target, the fairness verdicts, the ADR-015 search benchmark and the targets
that load generation cannot observe. The exit code is non-zero when any target
fails, so a regression is caught. `--inject-slow-query-ms` delays every patient
patient lookup statement inside the database to prove a slow query is recorded
as a failure.
The Locust file `ops/perf/locustfile.py` drives the same pages over HTTP
against a locally running synthetic server.

## ADR-015 search benchmark

ADR-015's revisit trigger is a measured p95 miss on blind-index patient search
in this harness. The report's `search_benchmark` records the blind-index p95,
the substring registry p95 and the verdict. The verdict is
`not_evaluated_dataset_below_100000_patients` on any profile whose focus tenant
is smaller than 100,000 patients, so only P-large-tenant can trigger it.
