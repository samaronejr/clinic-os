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
