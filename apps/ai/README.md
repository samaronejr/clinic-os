# Governed synthetic AI gateway

## Revenue-cycle assistant (todo 60, AI-06)

Default off: `revenue_cycle` has its own organization opt-in, evaluated model
configuration and A1 agent policy. `request_revenue_cycle`, `collect_revenue_cycle`
and `propose_revenue_action` are keyword-only and clinic-scoped. Current
`charge.read`, `tiss.read` and `tiss.manage` are required before any source lookup.
Finance reviewers additionally need `automation.finance` to admit, approve or
execute an effect; the shared source recheck enforces it on every path,
including the generic agent queue.
Policy publication remains an administrator action. Finance gains visibility
only to this capability's policy/run/proposal/approval/receipt ancestry through
`ai_revenue_row`; neither the general agent queue nor another capability opens.
PostgreSQL enforces the same boundary (ai.0011). A restrictive
`agent_finance_bound` policy on the seven agent tables requires `ai_revenue_row`
for revenue ancestry, so agent staff without `automation.finance` neither read
nor change those rows. A revenue vote stays current only while its approver holds
`automation.finance`, and `tiss.manage` snapshots approve revenue proposals only.
Publishing any revenue policy version, the first included, also needs the
publisher's current `automation.finance`, in Python and in PostgreSQL.

Retrieval uses the existing FORCE-RLS insurance and journal tables, under their
writer locks. One lock order holds: gateway budget, agent send, insurance, then
journal; commands take only the prefix they need. The immutable guide binds billed procedure codes, quantities and
prices to its completed encounter. Only those minimum billing facts, exact
centavos, posted-allocation totals and fixed source aliases enter the model.
Patient/card/encounter/member identifiers, names, professional identities,
clinical notes and raw payer prose never enter the prompt. Source identifiers,
versions and hashes stay in the existing encrypted, requester-bound AIArtifact.
Absent journal terms or posted allocations are cited by the exact scoped book
query result, never by an invented receipt.
Output keeps the input's original one-day deadline. There is no new tenant table.

Rules detect recorded denials, missing return/recognition/payment evidence and
claim/journal or claim/allocation discrepancies. Missing evidence means not found
in these supplied sources; the assistant never claims absence of care. Clinical
supporting evidence is explicitly not retrieved. The synthetic model classifies
only these findings; the output envelope has no narrative/instruction field.
Validation compares the entire output, exact citations and every
procedure/code/quantity/price against the encounter-bound source. Fabrication,
upcoding, omitted findings, extra tools and unknown fields reject the entire
response before output storage. Display text and administrative appeal wording
are rendered from the verified facts. Payer return text cannot choose a tool.

`appeal.draft` and `discrepancy.explain` are A1-only registered commands. Finance
first previews one exact effect, then explicitly approves its canonical digest
using the shared todo-39 TOTP attestation, authority snapshot and execution
algorithm. Approval and execution reload source bindings, policy, provider and
authority; new returns, allocations, reversal, supersession, expiry or revocation
refuse stale work. The direct draft bridge also requires the matching executing
intent and authenticated human approval. Replays retain one receipt/effect.
An appeal has no new clinical assertions and remains an unsubmitted insurance
draft; its separate human submission approval is unchanged. Its body is only
the reviewed wording: source identifiers and digests stay in the artifact and
on the review page, never in payer-facing text. An explanation's receipt
records review only. No claim, clinical record or journal is rewritten.

Refund, write-off, price change, payout and submission are unrepresentable on this
A1 path. A material agent command would need a separately registered domain
validator and todo-39 A3 approvals; no such command is authorized by AI-06.
The native private/no-store review page is Finance > Revenue-cycle assistant.
Selectors travel only in POST bodies with CSRF/TOTP and a manual workflow path.
Audit/outbox/logs retain metadata only; all content remains encrypted.

Migration `0010_revenue_cycle` follows AI 0009 and insurance 0001. It widens frozen
vocabularies, adds the A1-only policy CHECK and installs exact Finance scope
policies/resolver grants. Fresh reverse is supported only without retained
AI-06 rows; after history, rollback = restore or disable the organization flag.
Tests are `tests/ai/test_revenue_contracts.py`, `test_revenue_cycle.py` and
`test_revenue_views.py`, plus the existing exact posture and SQL registries.
Real adapters remain `real_enabled=False`; sandbox, paid, provider and live legs
remain **BLOCKED-ON-EG**. Local fixtures satisfy no external gate.

## Offline ASR/LLM benchmark (todo 48)

`python manage.py ai_benchmark --report <new-path>` runs only the closed
synthetic/recorded adapters against todo 47's hashed engineering corpus. JSON
front matter is validated by `benchmark.schema.json`; dated selection records
live at `docs/ai/provider-selection-<date>.md`. Local latency, ASR WER, gateway
validity, failure counts and per-encounter usage/cost are measured. Clinical
error rates remain null pending independent two-rater adjudication; no fake
metric authorizes a clinical configuration or represents vendor performance.

Missing/unverified region or retention terms and foreign-jurisdiction fallback
routes are ineligible. No eligible automatic fallback means manual documentation.
The optional `--record-selections` flag requires `clinic_owner` and appends only
synthetic `selected_in_plan` versions under `benchmark_<purpose>_<route>` keys,
without replacing runtime provider capabilities or creating approval records.
Paid, real-provider, sandbox and live legs remain BLOCKED-ON-EG.

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

PostgreSQL enforces the approval rules too (`0012_approval_attestation`).
Runtime cannot insert a vote: `agent_record_approval` is the only writer and
records one only after it rechecks in SQL that the approver's own session
carries a persisted TOTP or passkey verification from the last five minutes,
and that the evidence names the same approver, clinic, digest and evidence id.
The vote keeps that verification instant, and `agent_approval_current` counts
only such votes. A proposal declares its payload's quorum; the guard refuses
approval, an execution claim, the executing transition and the send unless
that many current votes exist and none is unverified or stale. Only a bounded
machine-principal policy may declare zero, and then only that machine principal
on the agent login may claim, start or send the execution. The HMAC attestation remains a
Python check because its key never enters PostgreSQL. Votes and proposals
recorded before 0012 lack this evidence and cannot advance; reject them and
propose again. Reversing is supported on an empty synthetic database only.

## Care coordination (todo 53, AI-05)

`care_coordination` is a clinical gateway purpose (patient-scoped
`clinical.read`, default off: organization opt-in, model configuration and an
enabled agent policy) and an agent capability with exactly one command,
`care_action.create`, at autonomy A1. It never diagnoses, prescribes, changes
treatment or messages a patient.

- **Request.** Only the plan's clinician (patient-scoped `clinical.write`)
  asks, for an active plan whose source note is still the finalized version.
  The encrypted input holds the plan section of that note and the plan
  identity; nothing else of the chart.
- **Model output is untrusted.** `care_contracts.validate_reply` is the only
  path to a proposal. The envelope is exactly `{schema, items}` (16 items at
  most); each item is exactly `{kind, start_days, end_days}`, with days counted
  from the plan's approval. Proposable kinds are `return_window` and
  `staff_coordination` (administrative) and `order`, `questionnaire`,
  `monitoring_task` (clinical). A referral destination or a patient
  instruction is clinician-written free text, and a medication, dose or
  procedure has no field at all: such an item is kept only as a refusal
  (`kind_forbidden`, stored kind `unsupported` for invented kinds) without any
  model text, which stays in the encrypted gateway output until retention.
- **`CareProposal` (`ai_careproposal`).** One row per model item: closed kind,
  audience, window, the plan revision and source version it cites, and a
  state: `pending`, `refused`, `approved` (staff), `accepted` (clinician),
  `rejected` or `stale` (`source_superseded`). FORCE RLS: the patient's
  clinicians see every row; staff who hold both the Automations queue
  authority (`automation.admin`/`.organization`) and `tasks.assign` see only
  administrative rows that are not refusals. Only the requesting clinician can
  insert, from their own succeeded run over the current plan. A trigger owns
  every decision: it binds the decider, requires the plan to still be current
  for approval or acceptance, and requires the exact care action (and, for
  staff, the A1 receipt) to exist. No DELETE, no TRUNCATE, no plaintext.
  Its `ai_care_plan_current` predicate is private to `clinic_resolver`; neither
  runtime role nor `clinic_owner` can invoke it directly to inspect a plan.
- **Staff bundle.** The preview lists every pending administrative item with
  its exact effect; the confirmation posts the preview's SHA-256 bundle digest,
  and any change refuses the whole bundle before an effect. After one recent
  TOTP verification, each item still becomes its own digest-bound
  `care_action.create` proposal, approval receipt and A1 execution, revalidated
  at the send boundary against the plan and the pending item. The operation can
  only express an administrative kind, so a clinical item is not representable
  in an agent approval. A stale or refused item never blocks the valid ones.
- **Clinician decisions.** A clinical item becomes a care action only when the
  patient's clinician accepts it through `add_care_action`, which records them
  as its approver; staff can only decline administrative items.
- **Stale sources (scenario 8 variant).** A superseded plan, a changed plan
  revision or a source note that is no longer finalized makes every pending
  item stale at its next approval or acceptance, in the service and again in
  the database.
- **Page.** Automations > Care coordination (`ai:care-coordination`) is a
  TOTP-protected, private no-store page whose selectors travel in POST bodies.
  The clinician sees their active plans, requests and refreshes suggestions and
  decides clinical items; staff with approval authority review and approve the
  pending administrative bundles. Every refusal keeps the committed state. The
  `care-coordination` browser suite owns these journeys and `bundle.webm`.

The synthetic provider replays only the reviewed recordings in
`fixtures/care-coordination-v1.json` for their exact plan texts (SHA-256);
any other plan proposes nothing and planning continues manually. Migration
`0007_care_coordination` adds the purpose to the four closed vocabularies,
replaces three resolver bodies by anchored edits of their released text and
creates the table with its posture. Its reverse is a rehearsal for an empty
synthetic database only; with history, rollback = restore. The A1 path checks
the same synthetic provider lifecycle record as the other A1 command. Live,
sandbox and real-model legs remain **BLOCKED-ON-EG** (EG-2, EG-3, EG-5).
Migration `0008_care_plan_status_private` also revokes the old helper grants on
databases that previously applied 0007. Reversing that reconciliation keeps the
corrected 0007 restriction and does not restore the disclosure.

## Front-desk agent (todo 54, AI-04)

Default off. The agent answers a clinic's patient-initiated inbox messages
(WhatsApp, email, SMS on the signed webhook path) only while both opt-ins hold:
the organization AI policy enables the `front_desk` purpose, and the clinic's
latest `front_desk` agent policy is enabled at A2 with a service principal that
still holds `agent.execute`. Turning either off stops every new turn; that is the
rollback switch. The same assistant answers replies in clinic-opened portal
threads. The live portal session supplies identity; every scheduling call
separately requires its current `booking` operation. A messages-only session
cannot gain booking authority through a challenge.

- **Turn pipeline.** The comms inbound hook queues one `FrontDeskTurn` for each
  stored, supported inbound message inside the authenticated delivery
  transaction (`clinic_app.ai_front_desk_enqueue`). After commit the runner
  re-enters `inbound_context` with the same proof and answers each pending turn.
  One runner per provider account works at a time (a session advisory lock held
  across the turns), so concurrent deliveries never work on the
  same turn.
  Its dedicated `locks` connection also retains `agent-send-v1` across those
  transactions, sharing the existing policy/principal/membership mutation fence
  and serializing the policy's action budget across provider accounts. Portal
  replies take the matching transaction lock before the conversation or audit
  lock, so their policy check, booking, provenance and audit commit together.
  Every bridge (`ai_front_desk_turn`, `_articles`, `_state`, `_verify`,
  `_finish`) requires the conversation's own ingest principal, the enabled policy
  and the turn's pending state. Nothing else writes the agent tables
  (`ai_front_desk_guard`), and a thread that waits for a person gets no turn.
  Execution repeats that ownership check. A handoff also records a permanent
  cutoff: already received turns remain history and cannot execute after staff
  resolve the handoff. The handoff clears the outstanding offer.
  Portal replies run the same bounded decision inside their patient transaction,
  using its enrollment-bound ownership rather than a provider proof. No staff
  or tenant GUC is installed, and the challenge bridge refuses portal turns.
  Each completed turn appends `ai.front_desk.replied`, attributed to the
  authenticated ingest principal or the portal session, with clinic and verb
  metadata only. A replay adds no turn, effect or audit event.
- **Untrusted text, closed intents.** Message text is data. A fixed word
  classifier, after comms `classify_inbound`, picks one closed intent; the text
  never names a tool, a record or an argument, so "cancel all appointments"
  calls nothing.
- **Safety.** Urgent or clinical text gets fixed safety text (call 192 in an
  emergency, otherwise the clinic) and the thread becomes `handoff_pending`.
  Clinical-owner approval of that copy is EG-3 and still pending. The comms
  triage has already put the conversation in the clinician queue (Clinical Inbox
  feed `question`). There is no clinical advice and no monitoring promise. The
  first reply of every thread discloses the automation and the ATENDENTE path to
  a person.
- **Identity.** Appointment data needs a verified identity. Staff must have
  linked the thread to the patient through a verified contact, and the person
  must give the enrollment's date of birth. The bridge compares it server-side
  against the encrypted field, allows three attempts, and on success mints a
  booking-only patient grant and session for 30 minutes, issued in the name of
  the administrator who enabled the policy. Contact possession or a name ("sou o
  paciente X") never verifies, and linked and unlinked threads get identical
  replies. After three failures the thread waits for a person.
  The challenge also rechecks the exact current verified contact against the
  conversation's encrypted destination and binds its ID and destination
  version. Every later channel session use rechecks that binding. Destination
  changes invalidate appointment authority even if the new destination is
  subsequently verified. Patient transactions share the existing contact
  mutation key, so a committed change cannot be overtaken by a booking.
  A channel turn retrieves appointments, applies its accepted effect and inserts
  its reply/provenance in one inbound transaction. During scheduling it uses
  only the touched patient session, with staff, tenant and principal settings
  cleared locally and restored before publication. The contact mutation key
  stays held through reply insertion; SQL rechecks all appointment answers,
  including those with no tool. Failed publication rolls the appointment back.
- **Tools and limits.** `scheduling.hold` binds the first real free slot of the
  same practitioner in the next 14 days to the thread for 10 minutes
  (`patient_slots`; nothing is invented). `scheduling.book_approved` moves the
  appointment only after the patient's explicit SIM to that exact offer, checked
  again against real availability under the patient session
  (`reschedule_patient_appointment`). A new booking request, or an appointment
  request without an upcoming appointment, offers the first real generic
  30-minute patient slot within the same 14-day bound, showing its practitioner
  and clinic-local time. Acceptance calls `book_patient_slot` with the turn's
  idempotency key. Clinics that require approval use the human path.
  No appointment is created before acceptance; no availability offers the
  existing waitlist path.
  Inbound enqueue binds each message to the already published offer it could
  have received. A queued SIM received before an offer was published cannot
  accept that offer; a fresh subsequent SIM is required. Acceptance must be a complete affirmative
  reply; a negation, question or qualified reply never books merely because it
  contains an affirmative word. `comms.send_approved_link` appends an
  answer's approved https link. Each tool needs its entry in the policy allowlist
  (closed schemas in `TOOL_SCHEMAS`) and room in the policy's daily action
  budget; otherwise a person takes over (`policy_limit`). When nothing is free
  the reply offers the clinic's waiting list through the team.
- **Reviewed answers (D-15).** `KnowledgeArticle` versions cover hours, prices,
  services, preparation and administrative policies. Staff with
  `configuration.clinic` draft and approve them, and approval retires the key's
  previous version. The agent answers only from approved versions, by
  deterministic keyword lookup (most matching keywords; a tie answers nothing),
  and records the cited version on the turn. No match answers "Não sei responder
  isso; estou encaminhando sua mensagem para a equipe da clínica." and hands off.
  Replacement approval appends each previous version's retirement event and
  the replacement's approval event in the same transaction.
  Answer text holds no e-mail, link or long number; a link lives in the approved
  link field (plain https, no query).
- **Staff surfaces.** Settings > Assistant answers (`ai:front-desk-knowledge`,
  `configuration.clinic`) and Messages > Assistant handoffs
  (`ai:front-desk-handoffs`, `demographics.read`). The handoff list shows the
  reason and the time only; the text stays in the conversation. Mark resolved
  returns the thread to the agent. Audit events:
  `ai.knowledge_article.{drafted,approved,retired}` and `ai.front_desk.resolved`.
  Opening a handoff uses native POST to `comms:inbox` with `action=show`; its
  selector stays in form bodies for opening, realtime refresh and subsequent
  handling. Mutations redirect to the selector-free inbox; selection is not
  stored in the session.
- **Replies.** The finish bridge stores the reply as an outbound message without
  a sender, with the turn id as its command id (comms `0011_agent_replies`). On
  the synthetic channels delivery is simulated (`provider_state=sent`); a real
  provider send of machine-originated messages is BLOCKED-ON-EG, because the
  outbox has no principal path. The hook therefore queues no turn unless the
  data mode is synthetic and the channel is a synthetic one. A portal reply is
  delivered locally in the patient's transaction and visible only through the
  existing enrollment-bound readers, which require its completed agent turn.
- **Schema.** Migration `0007_front_desk` adds three FORCE-RLS tables without
  patient text, column-limited staff updates and retained history. The reverse
  runs only on an empty synthetic database; once turns exist, rollback = restore.
- **External legs.** Real-provider sends and clinical approval of safety wording
  remain BLOCKED-ON-EG. Urgent messages already enter the human-owned Clinical
  Inbox through comms triage; no automated clinical reply or monitoring promise
  is introduced.

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
