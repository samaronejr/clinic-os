# Communications

Appointment reminders now use the task-13 integration outbox. This is a
**synthetic implementation, not approved real messaging**. Email, SMS and
WhatsApp have distinct adapters, templates, capability decisions and receipts.
Task-6's dated records still have no selected provider, credentials or accountable
sandbox approval. No setting turns a synthetic result into real approval.

## Scheduling and authority

A resolver-owned appointment trigger atomically records an immutable reminder
snapshot and its `IntegrationOperation` on booking or changed appointment times.
This covers staff and patient booking, including waitlist acceptance, without
installing a staff identity in a patient request. The assigned practitioner is
the stored reminder actor; task 13 rechecks that actor's active organization and
clinic membership before every send. The appointment mutation remains attributed
to its original staff/patient audit principal. The snapshot is the scheduling
receipt; task 13 appends delivery lifecycle audit events.

The default is **24 elapsed hours before the start**, only when that instant is
still future at booking. UTC instants and the clinic's IANA timezone are captured;
both schedule and logistics render in that clinic zone, not the browser zone.
There is no late catch-up reminder when the configured due instant has passed.
Owner/admin clinic settings select 1, 2, 6, 12, 24, 48 or 72 elapsed hours for
future bookings/rebookings. Existing outbox due times remain unchanged; changing
a preference or configuration does not retroactively create notices for earlier
bookings. Channel approval, destination verification and opt-in remain mandatory.

Only verified destinations with an explicit `appointment_reminder` preference
produce a reminder. The snapshot binds exact preference and contact versions.
Opt-out then opt-in, a reverified changed destination, cancelled/rescheduled
appointments, expired appointment times and revoked templates cannot resurrect
an old notice. Rescheduling preserves old receipts and creates a fresh operation.
Cancellation/rescheduling share the task-13 subject advisory lock; preference
changes share task 14's stable patient/channel lock. A committed revocation
cannot be overtaken by an in-flight send.

## Runtime

Run the existing Celery worker and Celery beat (`-A config.celery`). Beat calls
`comms.dispatch_due_reminders` every minute. The durable database outbox, not a
long-lived broker ETA, owns due time. The resolver returns at most 100 opaque due
operation IDs; workers resolve trusted stored scope. Rows survive missed broker
publishes and worker restarts. Transient retries wait at least one minute and
allow at most three provider attempts. Abandoned in-progress work uses task 13's
bounded reconciliation, never blind resend. Stable operation IDs are provider
idempotency keys; terminal receipts are preserved. Existing generic integration
operations are unaffected by the nullable `not_before` extension.

`COMMS_SYNTHETIC_CHANNELS=email,sms,whatsapp` independently enables those
**synthetic-only** adapters; the default is empty. Acceptance references always
start with `synthetic:<channel>:`. They mean synthetic acceptance, not delivery.
A delivery status requires a separately authenticated callback through the task-13
boundary. No real callback protocol or HTTP endpoint is invented for unselected
providers. `COMMS_REVOKED_REMINDER_TEMPLATES=email:1,whatsapp:1` revokes exact
channel/template versions independently. These are deployment configuration,
not a live owner-approval or clinic-settings UI.

The three version-1 templates receive only clinic name, appointment date/start/end
and clinic timezone. No notes, diagnosis, reason, patient name, body or destination
is stored in the outbox. Destination and rendered body exist only during adapter
preparation. Failed provider details are fixed codes, never exception payloads.

## Staff surface and verification

The agenda links to **Lembretes de consulta**. The manager-only, TOTP-protected
clinic view shows up to 100 recent reminders, queued/sent/delivered/failed/cancelled
states and synthetic labels, without exposing destination or body. Pending
ineligible reminders display cancelled immediately and are persisted cancelled
when the worker reconciles them. Sent remains distinct from confirmed delivery.
Refresh is a native keyboard-accessible link; there is no optimistic send button.

`tests/renewal/test_reminders.py` exercises real PostgreSQL/RLS and all three
channel contracts. The registered `reminders` browser suite books through the
patient surface, sends through a separate `clinic_app` worker, applies explicitly
synthetic authenticated receipts, revokes preferences through the contacts UI,
cancels through the patient UI and observes bounded provider failure. Its
accelerated due clock is test-only; real providers remain `waiting_external`.

## Action-operation contract (task 7)

The shared outbox now stores `OperationKind` independently of `channel`.
`OperationRequest` keeps the existing communication contract; SQL producers and
historical rows default to `communication`. `ActionOperationRequest` represents
non-communication work with no channel and a required lowercase SHA-256
`payload_digest`. The referenced domain record owns the typed payload; bodies,
clinical content, prompts and destinations never enter the outbox. Kind and
digest are immutable at the database.

`enqueue_operation` compares RFC 8785 canonical fingerprints of tenant, actor,
clinic, kind, channel, provider, subject type/id, retry limit, payload digest and
idempotency key. Identical replay returns the original operation without another
dispatch, including after a transition. Any changed term raises the payload-free
`IdempotencyConflictError`. Fingerprints are reconstructed from stored immutable
terms so legacy and trigger-created operations need no invented backfill.
Concurrent claims use the existing database uniqueness constraint.

Actions require `register_action_adapter` separately from communication adapters
and an explicit `register_subject_recheck`; either missing fails closed. Reviewed
adapters prepare under current human clinic authority, verify the referenced
payload digest and their domain's permissions/approval policy, then perform the
external effect after commit. Existing subject locks, revocation rechecks,
bounded reconciliation and fixed outbox audit events remain in force. This
transport is not a generic tool API, does not approve an action, and never
impersonates a human on behalf of a service principal. The later machine action
gateway must use its own principal-bound authorization and receipts.

## Omnichannel staff inbox (todo 51)

Staff handle WhatsApp, email, SMS and portal threads in one inbox
(`/clinics/<id>/messages/`). This is a **synthetic implementation**: no live
provider, credential, template approval or transport is approved, and nothing
here satisfies EG-1 (provider selection and owner approval). Every live leg is
**BLOCKED-ON-EG**.

### Threads, purposes and the window

A `Conversation` has one channel, one contact and one purpose:
`transactional`, `care` or `marketing`. Channel, contact and clinic never
change. Marketing never changes purpose and never shares a thread with other
content; a transactional thread becomes `care` only when a clinician links the
patient (the one allowed transition, enforced by a trigger). A care thread always
has an enrollment, so an unverified number never receives care content. Inbound
numbers are linked only when the patient's **verified** contact (verified version
equals the current destination version) equals the thread's address.

WhatsApp free text is valid only inside the 24 hour customer-service window that
each inbound message opens or resets; the end is exclusive. Otherwise only an
approved template of a category that fits the purpose may be sent
(utility/authentication for transactional or care, marketing only for marketing).
`inbox_rules.py` holds these rules as pure functions; `send_message`, the
worker's send-time recheck and the PostgreSQL message trigger each repeat them.
The trigger judges the window from the row's own `created_at` and reads no clock.

Meta's pricing page (re-read 2026-10-29) charges delivered template messages by
category and market, charges service messages after 1,000 free per phone number
per month, and charges utility messages inside the window again since
2026-10-01. The plan's 2026-07-01 rate-card reference is therefore stale.
`MessageMeter` records the expected category when queuing and the provider's own
`pricing` object (category, billable, type) from the delivered status; no price
is hardcoded and BRL rates need the rate-card CSV (EG-1, unverified).

### Sending

`send_message(*, clinic_id, conversation_id, command_id, template_version_id=None,
body=None, parameters=())` queues one `Message` and one outbox operation in the
caller's transaction; `command_id` is both idempotency keys. A repeat returns the
original receipt, a changed payload is a conflict. Delivery state is derived: the
outbox lifecycle (queued/sent/failed) combined with the provider's monotonic status
(`delivered`, `read`, `failed`); a failure never overwrites proof of delivery.
A failed message offers `retry_message` under a fresh operation; the stable
operation id stays the provider idempotency key, so recovery does not duplicate.
Roles per purpose: transactional - clinic admin, clinic manager, receptionist,
scheduler, nurse and physician; care - nurse/physician with patient-scoped
`clinical.read`; marketing - clinic admin and clinic manager with a registered
consent source. The owner role is deliberately not in any list: the Messages
destination and routes are gated by `demographics.read`, which the owner bundle
lacks, so an owner gets no link and a 403, and the plan's authorization for this
todo is reception, nurse and physician by purpose. None exists, so every
marketing send fails closed with `marketing_opt_in_missing`; this todo adds no
marketing purpose, because the consent and ethical-CRM work (todos 20 and 62) owns
it and registers its source with `register_marketing_opt_in`.

### Inbound webhooks

`POST /comms/webhooks/<whatsapp|email|sms>/<account-uuid>/` is in
`BYPASS_PREFIXES` (D-19). Order is the contract: HMAC-SHA256 over the exact raw
bytes with the account's secret (`comms-inbox-<uuid hex>`, 64 hex characters) runs
before any parsing or query; then `comms_resolve_inbound` maps the account and
the secret's digest to the stored organization, clinic and a service principal of
purpose `messaging_ingest`; a payload that names another provider identifier is
ignored and any clinic named in a payload is never read. Each event id is recorded
once in `InboundEvent` in the same savepoint as its effect, so replays are
acknowledged without a second message and an event that cannot be applied yet
(for example a status before the send reference is stored) is not consumed.
`GET` answers Meta's subscription handshake from `comms-inbox-verify-<uuid hex>`.
Accounts are owner-provisioned (the runtime role only reads `ProviderAccount`);
this todo ships no owner CLI, only the synthetic provisioning in
`tests/comms/support.py` and the browser fixture. The per-account secret store
shape (exactly 64 hex characters, digest-matched in `comms_resolve_inbound`) does
not fit a real Meta app secret; mapping real secrets belongs to live activation
(todo 77) and stays BLOCKED-ON-EG.
The Cloud API field shapes follow Meta's reference; email and SMS use one documented
signed envelope until a provider is selected.

### Clinical advice and AI

`triage.classify_inbound` is a conservative pt-BR router: urgent or clinical terms
set `needs_clinician` (shown in the Clinical Inbox as feed `question`), and only
positively administrative text may receive an AI draft. `suggestions.py` stores
the draft encrypted for review; a draft that itself contains clinical terms is
dropped before storage, and `use_suggestion` is an ordinary `send_message` by a
signed-in human with its own `command_id`. No path sends a model result. The
governed gateway wiring (purpose `front_desk`, A1) is not registered: until an
approved drafter exists the panel reads "Sugestão indisponível".

### Audit and telemetry

Staff commands append fixed `comms.*` events (no content). Machine ingest appends
`comms.inbound.message_received`, `.escalated`, `.status_applied` and
`.template_status_applied` through the provider principal, the same pattern as
`orders._lab`: `comms_audit_scope` proves the record belongs to the current
principal's account and tenant, and `comms_ingest_audit` appends the hash-chained
row with the principal as actor. Payload keys are only `clinic_id` and
`object_verb`; no body, address or label. A replayed or foreign-claim delivery
appends nothing, and the outbox state machine is untouched: the inbox's delivered
and read states stay derived from `Message.provider_state`.

### Contact tokens

`Conversation.contact_fingerprint` is the tenant blind index of
`<channel> NUL <canonical address>` (purpose `comms.conversation.contact`).
New threads store the newest data-key version's token and every lookup matches all
versions, so neither an application secret rotation nor a data-key rotation
orphans a live thread.

### Portal

The portal channel is the in-app thread. Staff open one for an enrollment from
the inbox ("Start a conversation", channel Portal) and send free text; the outbox
operation succeeds without leaving the system (`PortalInboxAdapter`), and only
then does the patient see the message. Comms owns both sides: the staff pages
above and the patient page `/patient/messages/` (`comms:patient-messages`,
`portal_views.py` over `portal_messages.py` and
`migrations/_portal_messages_sql.py`). The intake portal only lists it as its
`messages` destination (todo 50).

- **Who reads and replies.** A patient session that holds the `messages`
  operation, or a verified, active delegate whose grant names `messages` and the
  `care_processing` purpose (the effective scope of
  `clinic_app.current_patient_authority`). New invitations grant `messages`;
  invitations and sessions issued before it do not, and the patient needs a new
  invitation. A delegate reads every portal thread of the represented patient:
  threads carry no confidentiality flag, so staff keep representative-restricted
  content out of the portal channel.
- **What the patient sees.** The portal threads of the session's effective
  enrollment, latest message first (at most 50 threads, and the newest 200
  messages of a thread): its own side of each thread and the clinic messages
  whose latest outbox operation succeeded or delivered. Queued, cancelled and
  failed messages, internal notes, suggestions and every other channel stay out.
  Times render in the clinic's zone; nothing is cached in the browser.
- **Replies.** The patient replies in a thread that is not closed, and a pending
  thread reopens. Starting a thread stays with staff. The thread selector and a
  per-page form key travel in the POST body: a repeated key returns the original
  receipt, other text under the same key is a conflict, and a closed thread
  refuses the reply and shows its text back. `clinic_app.comms_patient_reply`
  stores it as an inbound message (`event_id` `portal:<key>`, no sender, no
  command) with the tenant envelope, triaged by `classify_inbound` as provider
  inbound is (clinical or urgent text sets `needs_clinician`, which only rises),
  and opens no service window. The message guard repeats the rule: an inbound
  portal row needs the live patient session of the thread's enrollment and a
  thread that is not closed, and refuses any staff actor.
- **Denial and audit.** Unknown, foreign-patient, foreign-clinic and malformed
  selectors render one access gate. A reply appends
  `comms.portal.reply_received`, and `comms.portal.escalated` when it first flags
  the thread, with the patient session as actor and only `clinic_id` and
  `object_verb` in the payload. Reading is not audited, as on the other portal
  pages.
- **Staff side.** The reply shows in the staff thread as the patient's and
  publishes the `clinic:<id>:messages` hint (and the Clinical Inbox hint when it
  is clinical). A representative's reply reads the same there; its audit row's
  session tells them apart. The patient page has no live updates.
- **Urgent replies and coverage (todo 55).** A newly stored reply that
  `classify_inbound` marks urgent runs the hooks registered with
  `register_urgent_reply_hook` (called with the message id) in the reply's own
  transaction under the same patient session; a failing hook rolls the reply
  back. Workflows registers its urgent-concern queue there. The message list and
  thread state the clinic's configured urgent-message coverage
  (`apps.workflows.urgent.patient_coverage_text`), and without a configured policy
  they never claim coverage.
- **Send checks (todo 55).** `register_message_send_check(check)` lets the domain
  that queued an outbound message veto it at send time: each check runs after the
  channel rules inside `message_send_eligible`, in the send's tenant transaction
  under the sender's authority, and any `False` cancels the operation without a
  provider call. Workflows registers the release-notice scope recheck.

### Staff entry points

- **Workflow selections.** A workflow opens a thread with `POST` to the inbox,
  `action=show` and `conversation_id` in the body. Its subsequent actions keep
  that selector in their bodies, and realtime refresh uses the same POST.
  Each selected-thread request rechecks the actor's current thread authority.
  Mutating actions redirect to the inbox so reloading cannot repeat the command
  or put the conversation selector into a navigation URL.
- **Realtime.** Both pages declare `data-realtime-topic="clinic:<id>:messages"`
  and refetch their own region on `rt:messages` and `rt:poll` (HTMX `hx-select`).
  The element that carries the trigger stays in the page and only its children are
  swapped (`hx-swap="innerHTML"`): HTMX binds a swapped-in element's trigger after
  its settle delay, so replacing the element would drop a hint that lands in that
  gap. A new inbound message, a status or another agent's action then appears without a
  reload; reply, suggestion and note forms sit outside the refreshed region and keep
  what staff typed. The stream needs the per-user lease that
  `grant_clinic_topic(kind="messages")` stores; without it the page falls back to
  polling.
- **Linking and verification.** An unlinked, non-marketing, non-portal thread shows
  a "Link to a patient" panel. Reception and clinic admins search the registry by
  exact full name (`search_patients_exact`); clinicians, who have no registry
  search, pick from their own active care team (`care_team_patients`). Each
  candidate offers the link action (clinicians may also move the thread to care) and,
  for reception, a form that opens that patient's contact page to save or verify
  the contact. Search terms and enrollment ids travel in POST bodies only; the
  service still refuses an unverified or different number.
- **Starting threads.** The inbox page has a "Start a conversation" panel with the
  same patient lookup and a channel and purpose choice limited to the purposes the
  actor may open. Care threads are therefore reachable by nurses and physicians only,
  for patients on their care team.
- **Quick replies.** Active snippets of the thread's purpose are listed above the
  composer; choosing one fills the message box and nothing is sent.
- **Language.** Page copy, refusals and labels are English msgids with a pt-BR
  catalog (`locale/pt_BR`); tests assert `gettext(msgid)`.

### Tracked follow-ups

Each item is known and not scheduled; none is needed for the acceptance or QA paths.

- **Template and provider-account management.** Template create, approve and submit
  have no surface, and with no owner CLI (decision 3) templates, quick replies and
  provider accounts exist only through fixtures and services.
- **One `command_id` per page.** A thread page issues a single fresh id that its
  send, suggestion and retry forms share. Every action redirects to a new page with
  a new id, a same-key repeat returns the original receipt and a different payload
  under the key is refused, so a stale second tab fails closed; per-form ids would
  remove the shared key.
- **Failed-contract rehearsal.** The rollback rehearsal for migration 0008 is not
  recorded; its rollback is a restore (the migration is irreversible by design).
- **Portal follow-ups.** Patients cannot start a portal thread, staff see no read
  receipt for portal messages, a thread cannot be restricted from representatives,
  and browser notifications stay unavailable (`PUSH_NOTICE` in
  `apps/intake/portal.py`, BLOCKED-ON-EG).
