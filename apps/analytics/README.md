# Governed clinic metrics

Task 63 delivers fifteen versioned definitions, immutable read-model refreshes,
saved reports and the synthetic operations analyst. `definitions_v1.py` is
frozen with the initial migration. A changed formula requires a successor
version; no user or model can publish or execute a formula.

The plan's referenced U1 completion artifact was absent from this workspace.
The explicit definitions below and `docs/analytics/metric-definition-v1.json`
are the implemented interpretation, not a claim to have recovered that artifact.

Every metric requires `charge.read` and either `configuration.clinic` or
`configuration.organization` in the exact clinic. These are existing,
remove-only governed permissions: manager, Finance and organization roles do
not bypass narrowing. No permission is inferred from a clinic name or a model
claim. PostgreSQL repeats the decision before reading any numerator,
denominator, refresh, source or usage count. Both views restrict tenant and
clinic permissions; they expose only operational references, state, times and
numbers. Clinical text, patient names and encrypted contents never enter them.

The authorization fingerprint binds the requester, exact clinic, current
membership IDs, bundle versions and effective subtraction records. Reads
recheck authority before consulting a cache. Old snapshots cannot remain
visible after a role change, revocation, account deactivation, permission
removal or timed grant change. Saved reports retain settings, never authority.
Cross-clinic requests are refused as a whole. Benchmarking remains
**BLOCKED-ON-EG-2** for all actors, including organization administrators.

Definitions carry grain, the closed `sum`/`ratio` AST, numerator, denominator,
time basis, half-open window, clinic timezone, allowed dimensions and filters,
late-data treatment, freshness, permission, quality and source provenance.
Windows are at most 366 days. The browser submits local dates; the server
converts them to aware instants in the stored clinic zone. Filters cannot select
income, location, protected characteristics or another clinic. Utilization,
balance metrics and staff activation/retention use totals; the other supported
dimensions are local day and weekday. Money remains exact numeric centavos,
never floats.

No-show rate is no-shows divided by completed plus no-shows. Cancelled,
expired, held and requested appointments do not enter that denominator.
Unresolved bookings explicitly make the result partial. Utilization compares
booked practitioner duration with unretired practitioner availability, clipping
both to the window; resource capacity and buffers are not interchangeable
denominators. Waiting time is arrival-to-first-call, and task turnaround is
creation-to-done. Result acknowledgment measures each matched source version.
Follow-up overdue counts nonterminal follow-up deadlines before refresh time.
AI correction time is suggestion-to-modified-review elapsed time, including
queue time; it does not claim to measure active editing seconds.

Finance comes only from task 56's posted journal entries and lines. Receivables
are cumulative patient and insurer A/R debit balances. Verified collections
are posted `collection` asset debits; observed/unverified settlements are
excluded. Net collections subtract confirmed refund/chargeback asset credits,
before PSP fees. Payout liabilities retain both practitioner payables and
payout-in-transit credit balances, including unknown sends. Missing books are
unavailable, not zero. Generic payer adjustments cannot distinguish adjudicated
denials: the denial metric is explicitly null/partial until its journal posting
classification exists. Support load is likewise null/partial because this base
has no customer support-case source. Neither is silently substituted with
unrelated records.

Staff activation is current active exact-clinic staff with an operational
scheduling, EHR, task or billing audit event divided by current active staff.
Analytics report activity cannot inflate activation. The owner's scorecard explicitly
requires retained **paid** usage. That definition is null/partial until task 64
supplies subscription and metered-usage evidence; ordinary staff activity or
clinic journal receipts cannot stand in for paid SaaS usage. These are
engineering measures, not customer-research or growth claims.

The `metrics_refresh` workflow step pins a metric and 1-366 days to its run's
creation time and step UUID. The established workflow engine owns claims,
fencing and retries. Refresh and receipt commit together; late data requires a
new command. Refreshes are append-only and rebuildable from approved views.
After 15 minutes the UI shows stale data with `dados de HH:MM`; missing sources
and unresolved rows remain explicit. A zero denominator yields null, not zero.
Forecasts are displayed as unvalidated/unavailable, never as observations.

The analyst has no SQL, shell, HTTP, filesystem or financial tool. A small
deterministic synthetic vocabulary resolves one catalog name; ambiguous or
unrecognized text asks for clarification. Raw questions are never retained,
logged or sent to a provider. The gateway receives only a validated descriptor
and authorization fingerprint, and the output contains exactly the six fields
in `docs/analytics/query-descriptor-v1.json`. Broadened scope or model output
outside that schema is rejected before storage.

Inference reuses the governed AI gateway's encrypted artifacts, reviewed model
configuration, provider lifecycle, organization opt-in, budget, outbox,
after-commit dispatch, kill switches and content-free metering. Admission,
dispatch, artifact access and display recheck current authority. The model
only proposes; an explicit human acceptance binds the exact descriptor digest
and authorization version before a read-only refresh. Rejection is retained.
No clinical or financial record can be changed by this analyst.

All provider, sandbox, paid and live legs stay **BLOCKED-ON-EG**. The only
runnable adapter is deterministic and offline; no synthetic evidence clears
an external gate. UI uses native forms with optional HTMX, POST selectors,
English msgids and pt-BR catalogs, and no browser content storage.

All four tenant tables use FORCE RLS. Definitions and aggregate snapshots
are resolver-written; saved reports and review decisions are validated on
insert. Updates, deletes and truncation are refused. Grants, resolver result
types, audit events and the recovery manifest are pinned from the live catalog.
Migrations are additive; an unused installation reverses its schema and restores
the previous workflow validator without removing pre-existing resolver grants.
Metric history or a retained metric workflow refuses reversal before removing its
objects. Rollback after retained writes is restore; read models may be rebuilt.
