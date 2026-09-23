# Billing

Task 37 provides scoped invoice services, not a payment provider or billing UI.
`create_invoice` binds the current organization, an authorized clinic and its
patient enrollment, with optional matching appointment/encounter IDs. Clinical
content is never read or copied. Financial inputs are positive integer BRL
centavos (up to PostgreSQL bigint); floats, Decimal values, zero, negatives and
other currencies are rejected rather than rounded.

Every creation names its operation with a caller-supplied `idempotency_key`
that is unique per organization in the database, so replaying one submission
returns the charge it already created instead of opening a second one. The
stored `create_fingerprint` covers the clinic, patient and exact BRL terms:
reusing a key for different terms raises `BillingIdempotencyConflictError`
rather than resolving to the first charge, and two intentionally identical
charges remain possible under their own keys. Neither column is updatable by
the runtime or resolver role.

The existing owner, clinic_admin and receptionist assignments authorize billing
within their exact clinic. Physicians have no billing authority by that role
alone. FORCE RLS and runtime grants enforce this independently of the services.

Draft edits use expected revisions and retain database-generated snapshots.
`issue_invoice` freezes amount, currency, reference and scope. Charges progress
from draft to open, then paid or cancelled; paid charges cannot be cancelled.
`release_invoice` separately makes an issued charge and its eventual receipt
available to its patient. New patient invitations include the `billing` operation;
previous grants/sessions are not upgraded. `patient_charges` revalidates the live
session and returns only its own released financial projection, with no clinical,
internal settlement or staff fields. Direct patient table reads return no rows.

`confirm_settlement` is an explicit billing-staff attestation of an independently
confirmed **manual** settlement, identified by an opaque evidence UUID. It is not
a provider callback handler and must not be used to infer payment from charge
creation, an unchecked webhook or synthetic adapter success. The database checks
exact issued terms, locks the invoice, retains the confirmation actor/time and
creates one immutable receipt in the same transaction. Exact retries return the
same receipt. Real provider authentication/reconciliation remains unavailable
under task 6 and belongs to task 39. No provider or live readiness is claimed.

Task 38 adds `apps.billing.pix.prepare_pix_charge` and `complete_pix_charge`.
Both require current exact-clinic billing authority. The task-6 PIX record selects
no provider: Asaas is only a historical candidate. Real creation is permanently
disabled, including with credentials or an asserted runtime approval flag. No
credentials are accepted, persisted, returned or sent by this implementation.
Only `CLINIC_DATA_MODE=synthetic` plus explicit Django setting
`BILLING_SYNTHETIC_PIX=True` permits the local rehearsal adapter (default: off).

Prepare an immutable request, commit its tenant context, then complete it in a
subsequent context. Stable invoice identity, not arbitrary browser retry keys,
selects the original operation. Exact integer amount, BRL, invoice reference and
30-minute expiry bind a deterministic synthetic provider reference and exact PNG/
copy text. Every QR encodes `SYNTHETIC-NOT-PAYABLE`, not a payable PIX BR Code.
Completion verifies the entire response before storing it and never marks an
invoice paid or creates a receipt. Retried/concurrent completion returns the same
stored result. A timeout after local generation leaves the committed request for
exact replay; there is no external side effect to reconcile.

An expired request/result is never edited. Pass its `previous_id` to prepare a
linked successor; retries of that regeneration converge. Expired pending local
requests can also regenerate because no external charge exists. This rule must
not be reused for ambiguous real-provider timeouts. FORCE RLS, exact clinic
predicates, invoice-binding triggers and insert/select-only grants protect the
append-only operation/result history. Patient QR presentation belongs to task 40.

Before adding a real adapter, obtain provider/owner/account/package approval and
pin its current official sandbox charge, QR, expiry and exact idempotency/lookup
contract. A real adapter needs an out-of-transaction durable send and authoritative
reconciliation path; it cannot replace the local adapter in this transaction.
The core create-idempotency helper names patient, scheduling and charge-creation
payloads; PIX instead uses immutable invoice binding plus unique root/successor
constraints.

Task 39 adds `apps.billing.reconciliation`: `receive_payment_event` authenticates
the raw provider event through the registered `PaymentEventAdapter` before any
stored charge or tenant is resolved, then resolves tenant, clinic, invoice and
the recorded operation actor only from the stored `(provider,
provider_reference)` pair. Payload tenant claims are never read. Each event is
verified against the adapter's authoritative charge lookup (run with no open
transaction) before settlement; `reconcile_charge` recovers a missed event
through the same verified path. Because the lookup precedes the per-operation
lock, its answer is rechecked under that lock: a newer recorded event with
different authoritative terms (status, amount or currency) supersedes it and
is recorded `operator_required`/`superseded` instead of settling. Exactly one
immutable `PaymentEvent` row is recorded per `(provider, event_id)`;
redelivery converges. Recovery attempts mint a unique generated event id and
deduplicate on the recorded authoritative terms, so a superseded or rejected
attempt never permanently consumes the recovered facts. Only a verified exact
settlement creates the settlement and its receipt in one transaction. Expiration, cancellation, reversal, mismatched or
overpaid amounts, unverifiable claims, superseded observations and rejected
settlements are recorded as explicit `operator_required` states with a fixed
reason vocabulary; reconciliation never sends money, cancels an invoice or
refunds. The synthetic adapter authenticates rehearsal
events with the `BILLING_SYNTHETIC_PIX_SECRET` HMAC setting and reports only
pending/expired authoritative states, so a synthetic settlement claim is always
flagged `unverified_settlement` and can never create a receipt.

No insurance, fiscal tax invoice, refund automation or clinical/provider metadata
payload is added. Financial history cannot be edited or deleted by the runtime
role.

Task 40 adds the payment surfaces. `apps.billing.presentation` projects stored
rows into one closed display vocabulary - `draft`, `issued`, `pending`,
`expired`, `flagged`, `paid`, `cancelled`, plus the patient ledger's summary
`open` - and `apps.billing.views` renders the staff ledger and charge screen
plus the patient charge and receipt screens. Presentation reads committed state
only: it never calls a provider, never writes, and `paid` requires the stored
receipt row, so creating a charge, refreshing a page or an authenticated but
unverified event can never render success. Reconciliation is deliberately not
reachable from a view: a request already owns its tenant transaction, and the
authoritative provider path requires an outermost one.

Every state change is a POST that redirects to its safe GET, so refresh, back
and retry re-read state instead of creating a second charge. The create
operation is named by the server - signed-in actor and session, clinic,
patient, exact BRL terms, and the charge this submission deliberately repeats -
instead of a value minted per rendered page, so the form a browser restores on
Back recomputes the same organization-unique name and resolves to the charge it
already opened. Editing the value names a different charge and still opens it
exactly once. A genuinely second identical charge stays possible as an explicit
act: "Criar outra cobrança igual" prefills the create form with the charge it
repeats, which changes the operation name, and that repeat submission is itself
retry-safe. The charge action resolves the request to act on from stored
rows: a live request is completed again and returns its exact stored result,
and only an expired one gets a linked successor. Payment instructions render
only while paying still does something: a cancelled or paid charge keeps its
stored code in history but offers neither the copy text nor the QR. Status refresh is bounded by the server: each response emits
the next refresh hop with an incremented attempt and stops emitting it at the
limit, leaving the manual refresh control that also works without JavaScript. A
refresh that fails says the screen may be stale; it never changes the state.

Migration `0008_invoice_create_idempotency` adds that create key and
fingerprint to `billing_invoice` with their unique organization constraint.
Migration `0007_patient_charge_detail` adds the resolver
`clinic_app.billing_patient_charge(uuid)`. It repeats the exact session
predicate of `billing_patient_charges` - live unrevoked session, matching
organization, clinic and patient, `billing` operation granted - and returns
only public payment fields plus the current copy text, QR bytes, expiry, a
flag that an event still needs clinic verification, and the clinic time zone
every displayed time is read in. Another patient's identifier resolves to
nothing, and unreleased or undrafted charges stay invisible.

Payment copy carries no clinical text and no provider implementation metadata:
no encounter or appointment reference, no provider name, event id, reason code
vocabulary or transport detail. The QR is rendered from the stored bytes and the
copy text is selectable without JavaScript; the clipboard button appears only
when the browser can write to the clipboard. Both carry the synthetic rehearsal
badge, because the code is explicitly not payable.
