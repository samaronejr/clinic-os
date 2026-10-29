# Governed synthetic AI gateway

## Optional decision-support panel (todo 46)

The encounter panel is off by default. Organization enablement uses the existing
`decision_support` AI policy and budgets. Each clinician must opt in for each
request; the checkbox resets and creates no persistent preference or session
mutation. Missing or refused patient AI-use disclosure also blocks the panel.

Three current provider-registry records must carry the indicated `record_ref`,
a same-capability owner approval with nonempty evidence, and state
`production_authorized` or `activated`:

| Capability key | Required record |
| --- | --- |
| `decision_support_safety` | EG-3 |
| `decision_support_regulatory` | EG-4 |
| `decision_support_references` | EG-13 |

The same gates are checked at direct artifact access, admission, dispatch and
display. Research/sandbox state, revocation, missing evidence, unavailable catalog
or disabled policy renders unavailable. There is no gate-bypass setting, new
Celery task or direct runtime grant. Synthetic owner fixtures in disposable test
databases simulate these records; they do not satisfy an external gate.

The versioned catalog in `fixtures/reference-catalog-v1.json` contains only
self-authored synthetic CC0 evidence-review content, not a clinical knowledge
base. Its population and limitations are displayed alongside source and date.
The model may select only two closed review considerations with exact chart and
catalog references. Patient facts, reference statements and model hypotheses
remain separate. Free-form diagnoses, prognosis, prescriptions, dosing arithmetic
and patient-facing text have no representable output field. An invented citation
drops the entire response before storage and increments the existing invocation
`invalid_output` failure count. Source permissions and versions are resolved again
for each display; revoked facts and their dependent considerations disappear.

Inputs and outputs are encrypted encounter/session-bound artifacts with the
input's original one-day deadline. `accepted`, `modified` and `rejected` record
AI-use provenance against the exact invocation; none changes a clinical record,
creates a prescription, runs an agent tool, sends a message or affects care.
Modification is a clinician's explicit choice to edit in the manual clinical
record, not an automatic rewrite of the immutable model artifact. Refusals roll
back their input, chart-read audit entries and publication callbacks.

This is the binding contract for todo 38. All inference is offline simulation.
No model configuration, organization opt-in, command, or test satisfies an
external gate. Real adapters remain `real_enabled=False`. Live, sandbox, paid,
and real-provider legs are **BLOCKED-ON-EG**.

## Admission and authority

`invoke(*, clinic_id, purpose, principal, inputs_ref, budget)` accepts an
encrypted, immutable input artifact reference, never a prompt body. `principal`
must equal the current human user GUC. It is an assertion, not authority.
Clinical purposes require current patient-scoped `clinical.read`; front desk
requires `appointment.read`; analysis requires `charge.read`. Unknown, foreign,
expired, and another user's artifact all return the same access denial.

This gateway uses todo 7's human-attributed `ActionOperationRequest` transport.
The existing v1 machine principal grant set permits only `appointment.read` and
cannot impersonate a human, read clinical artifacts, or enqueue this action.
The separate agent execution platform owns v2 machine action authority; inference
admission does not widen those grants or impersonate a human.

Inputs and outputs are purpose-bound `EncryptedTextField` envelopes with explicit
retention classes and expiries: one day for synthetic transient input, seven
days for synthetic review drafts. These are synthetic rehearsal defaults, not
an approved live retention policy. Artifacts are immutable and are never
included in logs, task arguments, metering, audit payloads, or error messages.
The retention workflow owns eventual purge and legal-hold decisions.

## Approved configurations

`ModelConfiguration` is platform data: owner-written, runtime SELECT-only.
It stores purpose, capability key, provider/model, region and jurisdiction,
retention/training terms, integer micro-BRL prices, version, review reference,
primary/fallback route, timeout, and state. The DB permits only
`draft -> approved -> active -> retired`, with retirement also possible before
activation. Todo 47 additionally requires a passing, preregistered evaluation
and an explicit clinic-owner change approval before activation. The platform
owner still performs the model-state transition. Terms never mutate; changes
require a new version. Model review
references are synthetic configuration approvals, not provider/EG approvals.

Every load reads the provider lifecycle's current version and refuses missing,
revoked, degraded, or region-incompatible capabilities. The owner-approved
global region list and the fixed region-to-jurisdiction map must agree.
The only registrations are `synthetic` and `recorded_fixture`. Neither has a
network client. Both enforce the transaction/egress boundary in their base.
The recorded provider has one code-versioned recording, `fixture-v1`, not a
caller-selected filesystem path.

An open primary circuit permits selection of an active, approved fallback only
in the same jurisdiction class. It does not silently cross a region policy,
change a queued invocation's configuration, or retry an unknown outcome.
Timeout opens an organization/configuration circuit. Other organizations'
circuits are independent. A new reviewed model version starts closed; retained
circuit rows also support owner-only closure without deleting their history.
Only one circuit may be open per organization/configuration; another timeout
after closure creates a new retained circuit rather than overwriting history.
The reset uses the same organization send lock and records a system audit event.

Prompts live in `prompts/<purpose>/v<N>.md`. Admission records SHA-256 of the exact
bytes; preparation checks it against the outbox digest before dispatch.
Extraction accepts exactly `{"text": <string>}`, without duplicate keys.
All output remains untrusted draft data; the gateway executes no tools and
never changes a clinical or financial record.

## Budgets and metering

Organization-wide opt-in, capability enablement, and organization/purpose monthly
caps are stored in append-only `ClinicConfiguration.ai_policy`. The latest
organization snapshot governs every clinic. Ordinary clinic settings saves
carry the current organization policy forward under the same organization lock.
Changing it requires `configuration.organization` in every clinic.

Admission takes a per-organization advisory transaction lock and reserves the
worst input/cache mix plus bounded output and whole audio seconds. A DB trigger
repeats the cap arithmetic, so raw runtime inserts cannot bypass reservation.
All prices and aggregation use integer micro-BRL. Cached tokens are a subset of
input tokens and are charged once. `cost_minor` is the conservative ceiling to
whole BRL centavos; budgets aggregate unrounded `cost_micros`, never rounded rows.
Outstanding reservations count even across a month boundary. Completed usage
belongs to its settlement month in UTC.

Known pre-send refusals release the reservation. An unknown provider charge
(timeout, unavailable provider, invalid response) conservatively consumes the
reserved maximum; the reason code identifies that estimate rather than claiming
a verified charge. Synthetic costs are simulated, not money spent.

`AIInvocation` contains organization/clinic/user references, purpose, configuration
version, prompt hash, opaque references, ceilings, units, cost, latency, status,
reason, and timestamps. It contains no input/output text. Aggregate telemetry
uses the existing closed purpose/outcome labels and no tenant label.

## Dispatch and stops

The shared outbox owns after-commit publication, immutable replay identity,
operation/subject session-lock fencing, actor rechecks, and abandoned-send
reconciliation. Its registered action dispatcher routes AI to `ai-interactive`
or `ai-batch` (analysis). Recovery uses the same dispatcher. The AI task applies
organization fairness before claiming an operation. Only opaque operation ids
enter Celery. If core cancels before an adapter recheck, the AI task settles the
unspent reservation as metadata bookkeeping, including after role revocation.

Preparation decrypts within the existing tenant transaction. The inference call
runs with no open transaction on any initialized Django connection. Settlement
opens a fresh tenant context. Global and organization switches are rechecked
at admission and at the final send boundary. Organization policy mutations use
the outbox's stable organization mutation lock.

Every default is disabled: missing global control row and missing organization
or capability opt-in. Non-synthetic data mode refuses inference. The UI is a TOTP/CSRF
protected native form at `/clinics/<clinic_id>/settings/ai/`; it shows disabled,
degraded, validation, conflict, and unconfigured states without displaying
content or provider credentials. Clinical work remains available manually.

## Owner CLI

Run as `clinic_owner`, following the existing owner command environment:

```sh
uv run python manage.py ai_gateway global-switch --disable
uv run python manage.py ai_gateway global-switch --enable --approved-region br-synthetic
uv run python manage.py ai_gateway model-create --purpose front_desk \
  --provider synthetic --model-id synthetic-v1 --region br-synthetic \
  --retention-terms synthetic-no-retention --training-terms synthetic-no-training \
  --input-micros 3 --output-micros 5 --cached-micros 1 --audio-micros 7
uv run python manage.py ai_gateway model-state --configuration-id <uuid> \
  --state approved --approval-ref synthetic-review-v1
uv run python manage.py ai_gateway model-state --configuration-id <uuid> --state active
uv run python manage.py ai_gateway circuit-reset --organization-id <uuid> \
  --clinic-id <uuid> --configuration-id <uuid>
```

## Egress proxy contract

The current adapters make zero network calls. Deployment egress remains deny-all
for these workers. A future real adapter requires its own approved lifecycle and
live-runtime gates, an exact HTTPS hostname/port/path allowlist in its base, and
an outbound proxy which denies every destination by default, permits only the
approved endpoint, disables redirects and cross-region inference profiles,
verifies TLS, and logs metadata only. No wildcard, private-network destination,
caller URL, fallback hostname, or request/response body is permitted. That proxy
configuration is a future EG-bound deployment, not a claim made by this source.

## Schema and verification

`0001_initial` creates the app with exact ACLs, FORCE RLS, composite scope FKs,
reservation/transition guards, no-delete triggers, and no runtime truncate grant. Encrypted
storage makes the migration non-atomic and irreversible; rollback = restore.
The SQL policy installation itself is one explicit transaction. Recovery
includes all five AI relations and the tenant keys. Counts and exact membership
are derived from the live catalog by `tests/infra/test_recovery_manifest.py`.

`tests/ai/test_gateway.py`, `test_controls.py`, and `test_posture.py` are the
real-role runtime gates. `test_units.py` and `test_migration_state.py` need no
database. UI browser coverage extends the existing `clinic-settings` suite.
Root owns the serialized runtime slot, canonical gate, and independent review.

## Agent execution

Todo 39 adds versioned policies, canonical encrypted proposals, digest-bound
human approvals, unique fenced executions, receipts and compensation.
The binding [execution contract](../../docs/ai/agent-execution.md) describes
the closed tool registry, actual-principal attribution, execute-time revalidation,
unknown-outcome reconciliation and the synthetic-only limits.

## Permitted chart assistant (todo 44)

`context.assemble_context` reads only through the owning EHR, orders and task
services, with canonical physician and current patient `clinical.read`
authority before any source or coverage report. EHR source RLS includes the
current restricted-note grant. Bounded coverage describes available records
only; neither omitted record counts nor restricted-note existence are exposed.
The assistant performs no vector search, cross-patient lookup or tool action.

`brief.request_brief` and `brief.display_brief` implement generating, ready and
unavailable behind the existing default-off organization `brief` purpose flag,
global switch, model configuration and synthetic data-mode gates. Gateway input
contains the encrypted isolated question, a session HMAC and source references,
not copied chart text or previous turns. References are rechecked before send.
Output must exactly match `docs/ai/brief-v1.json` and the permitted input
references. Invented, omitted, altered or cross-patient citations fail closed.
Display resolves every reference and content digest again through the domain
services; revoked or changed sources disappear without an omitted-item count.
Every displayed assertion is the current source excerpt, with kind, record id,
exact version and date. Missing evidence is `not_found`, never clinical absence;
an explicit `none_documented` assessment has its own cited evidence state.

`POST /clinics/<clinic_id>/chart-assistant/` accepts exactly one
`enrollment_id` or `encounter_id`, a bounded `question` for `ask`, or an
opaque `invocation_id` for `display`; the native `generate` command requests
the whole brief. `surface` is only `today` or `workspace`. CSRF/TOTP, current
authority, session binding and no-store apply on every request. The drawer
works in the patient and encounter workspaces and the next-patient Today slot,
with native POST fallback and an explicit refresh while generating. It lists
the supported topical chart questions. Unlisted, identity-seeking and
instruction-bearing questions receive the same refusal without a patient
search, model call or action. They never confirm another person's existence.

One turn is retained only as existing immutable encrypted AIArtifact input and
output. The output shares the input's original one-day expiry, not a renewed
seven-day review deadline. Another login session, requester, patient or clinic
cannot display it; opening a workspace does not load old conversations. No new
table or SQL privilege is introduced. Rollback disables the `brief` purpose.
AI failure leaves manual chart work intact. All inference remains offline,
`real_enabled=False`; this implementation satisfies no EG or live approval.

## Document and result extraction

Todo 45 exposes `start_extraction`, `collect_extraction` and `review_extraction`
in `extraction.py`. Each command is keyword-only and clinic-scoped. Inputs come
from an immutable, identity-matched current `ResultVersion`, or from an
`available` clinical attachment through the EHR download authorization boundary.
Admission uses the existing gateway's extraction purpose, policy, budgets,
kill switches and after-commit outbox. No provider is called in a request.

`ExtractionJob` retains the exact source SHA-256, patient enrollment, page
rotations, requester and governed invocation. The DTO
`extraction_contracts.ExtractionCandidate` retains field, raw text, exact source
value, separately parsed exact string, explicit unit and reference interval,
one-based page and bounding box. Coordinates are unrotated top-left normalized
integers from 0 to 10000; highlights rotate with the rendered page. Candidate
content is encrypted in `ExtractionCandidateRecord`; no content enters audit,
outbox, task arguments or metering. Drafts expire with the seven-day synthetic
gateway output; retention owns eventual purge. Confirmed provenance is retained.

Decimal parsing requires pt-BR decimal comma. Dot-only separators, invalid dates,
unknown or missing decimal units and unreadable regions stay ambiguous. Dates
require DD/MM/YYYY and have no physical unit. No separator, unit, range,
abnormality, identity factor or clinical interpretation is inferred.
Only a candidate can become confirmed; ambiguous drafts may only be rejected.

The source-only synthetic adapter reads explicit PDF text lines
`field: value unit [reference interval]`. This bounded offline fixture format
is not general medical understanding. Raster-only pages receive an `Ilegível`
region. `recorded_fixture` model `extraction-fixture-v1` additionally replays
the exact synthetic PNG and reviewed regions in
`fixtures/document_extraction_v1.json`; another digest is refused.
General OCR, sandbox, paid, live and real-provider legs remain **BLOCKED-ON-EG**.

PDF tools are the existing `/usr/bin/pdfinfo`, `pdftotext` and `pdftoppm`.
Each local process has a ten-second deadline, bounded pipe output, no shell,
no network client, and discarded stderr. Plaintext travels only in memory.
Documents are limited to 32 pages and 10 MiB; the existing gateway's 1 MiB
encrypted-input plaintext limit also bounds the base64 document envelope.
Larger/unreadable inputs retain the manual path instead of claiming extraction.

Confirmation requires current patient-scoped `clinical.write` in addition to
source read authority. An operator needs that clinical grant; `order.route`
alone does not grant clinical write. An attachment can file only into a current,
independently identity-matched result version with identical source bytes and
enrollment. The clinician must first use the existing result ingestion and
identity verification workflow. The resolver checks the same bindings, expiry,
deterministic parse and exact encrypted confirmation payload independently.
It shares the result-series correction/review fence. One confirmation creates
one immutable `ResultObservation`, with `provenance="imported"`, confirmer,
candidate id and page/bbox. Replays do not create another observation.
Already-acknowledged versions cannot acquire new extracted observations;
confirmation never acknowledges, releases or closes the review task.

The native review form is `/clinics/<clinic_id>/extraction/`. Selectors travel in
POST bodies, with TOTP and CSRF. Authorized page bytes are rendered as inert,
inline PNG in the private no-store HTML body, not as a patient URL or active PDF.
Region links and per-value decisions work without JavaScript. No clinical
content enters browser storage, service-worker caches or data attributes.
Native 100/200/400-percent zoom keeps page bytes and region geometry together
inside a keyboard-scrollable source viewport, including on narrow screens.

Migration `0004_document_extraction` atomically installs the additive protected
tables, FORCE RLS, exact grants, cross-scope bindings, immutability/truncate guards
and the resolver-only observation bridge. Runtime cannot UPDATE a decision or
forge the bridge. The existing order observation ingestion rules remain intact
for every row without that exact uncommitted resolver marker.
It is non-atomic at the migration level and irreversible; rollback = restore.
Recovery includes both new relations and their encryption keys.

`tests/ai/test_extraction.py` and `test_extraction_state.py` are database-free.
`test_extraction_runtime.py`, exact posture, migration upgrade, catalog census
and the real native browser flow require the root's serialized runtime lane;
source-only evidence does not claim those gates passed.

## Evaluation and model change control (todo 47)

`evaluation/` is an offline engineering harness, not an external approval.
The closed `engineering-v1` corpus has twelve authored scenarios, four encounter
clusters and all eight AI capability identifiers. Its SHA-256 binds the exact
manifest and static audio bytes. The lawful-provenance field records synthetic
CC0 scripts and offline Flite generation. Tags describe simulated voices,
devices and adversarial conditions, not a representative pt-BR population.
The static speech registry is about 163 kB. Tests/CI only read and verify it;
`ops/testing/ai_evaluation_audio.py` is an explicitly invoked optional generator.

`EvaluationProtocol` freezes the corpus, case/cluster sample sizes and canonical
threshold document/hash before scoring. Registration requires one current human
with both physician and owner roles in the exact clinic. Only synthetic
engineering review references are accepted. Changing any threshold creates a
new version and hash; an older run cannot approve a change after supersession.
The engineering defaults are zero critical/forbidden errors, zero case-level
clinical errors, complete case-level citation/action correctness, twelve cases
and four encounters. These are software-test thresholds, not EG-3 clinical
thresholds selected after looking at a candidate.

`EvaluationRun` binds the exact configuration/version, model terms digest and
prompt/retrieval/schema source hashes. Scoring uses only the registered synthetic
providers through the unchanged `output_validation.call_provider` boundary,
outside all transactions. It never executes agent tools. Observations and
candidate output are encrypted with purpose `ai.evaluation.samples.v1`; they
never enter metering, audit, task arguments or telemetry. Every run remains
pending until independent structured human review completes.

Two distinct assigned physicians submit immutable `EvaluationRating` rows.
The run creator cannot occupy either rater slot or the adjudicator slot.
Their identities, model identity and the other vote are hidden in the native
review form at `/clinics/<clinic_id>/evaluation/`. The distinct adjudicator can
see the two votes only after both exist and resolves taxonomy disagreements.
RLS and raw-SQL guards repeat role, assignment, clinic, actor and ordering
checks. Record selectors remain in POST bodies; content is private/no-store and
never enters browser storage. One immutable binary taxonomy labels each case;
correction time is explicitly reported in milliseconds, not guessed by a model.
Correction summaries and encounter-bootstrap intervals use each case's mean of
the two independent raters' durations, including cases requiring adjudication.
The adjudicator's separate duration does not replace either rater's measurement.

The SQL resolver computes passed/failed from complete adjudication, sample
sizes, zero observed critical/unauthorized/duplicate/missed-escalation effects
and the registered rates. A caller cannot write a verdict column. Reports
retain numerator/denominator, WER alignment counts (descriptive only), taxonomy
counts, case-level citation support and action correctness, Cohen's kappa
(undefined when chance agreement is one), Wilson 95 percent intervals,
deterministic encounter-cluster bootstrap intervals and specialty/device/accent
strata. A synthetic pass and zero observed critical errors are not safety or
real-world performance claims. Real clinical thresholds, adjudication and lawful
representative data remain EG-3/EG-11.

`ModelChangeRequest` is requested/approved/rejected. Only a current clinic owner
other than its requester can decide it. A single-owner clinic must appoint a
second independent owner before an evaluated model change can activate; the
manual clinical workflow remains available. This deliberately accepts the
onboarding cost rather than a self-approval exception. Approval requires a
passing, current, unsuperseded run. A separate
database trigger refuses model activation without that decision; admission and
dispatch require the calling clinic's own current approval and recheck current
source hashes. A platform state transition needs at least one current clinic
approval, but it does not admit any other clinic. Registering a newer protocol
invalidates only that clinic's older approvals; another clinic's independent
approval remains valid. Model, prompt, retrieval or schema
changes therefore need a new configuration and regression approval. Existing
historical configurations with empty source bindings become unavailable rather
than receiving a fabricated approval. No provider or live-data gate is opened.

The owner command uses the same `assert_owner_database_role` entry check as
`ai_gateway`, then binds the explicit `--operator-id`/organization/clinic through
the existing transaction-local owner GUC context. It stays on `clinic_owner`.
The evaluation's resolver-owned role predicates are owner-callable; runtime-only
identity loaders are not granted to the owner and no runtime role is assumed.
The owner command requires explicit current human/organization/clinic scope:
`ai_eval register --capability AI-04 --corpus engineering-v1` registers the fixed
engineering rubric; `ai_eval run --capability AI-04 --corpus engineering-v1`
additionally names a configuration and three reviewers. `finish`, `report`
(`--html <new-file>`), `request-change` and `decide` use opaque run/change ids.
`ai_eval --help` lists the required scope options. Final platform activation
continues through `ai_gateway model-state`; an evaluation command alone never
activates a configuration.

Migration `0005_evaluation` adds four durable, FORCE-RLS relations, exact column
grants, composite scope bindings, no-delete/truncate guards and resolver-owned
functions. Encrypted observations make it non-atomic at the migration level and
irreversible; rollback = restore. Recovery includes all four relations and
their tenant keys. Test fixtures obtain scripted engineering approvals through
these same real services; there is no product bypass and those scripted votes
are not clinician adjudication or external acceptance.
