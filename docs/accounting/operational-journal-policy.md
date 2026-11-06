# Operational journal policy: completion-net-v1

- Status: implemented product policy; runtime acceptance pending.
- Recorded: October 23, 2026, for premium intelligence task 56.
- Architecture: ADR-011.
- Decision provenance: root's best judgment after the owner question timed out.
  **This is not explicit owner approval.**
- Missing evidence: the plan's referenced "U1 money section (draft)" was not
  recovered. Neither the rules below nor the new allocation corpus claim to
  reconstruct that missing advisory.

## Context and decision

Clinic books need one recognition event, one practitioner entitlement basis
and an exact reversal policy. Integer storage and balanced entries alone do
not decide those economic questions. The implementation records the supplied
root decisions explicitly rather than leaving implicit defaults in arithmetic.
Every new book stores `completion-net-v1`; account and policy definitions are
immutable. A changed financial policy needs a successor design and migration,
not an edit of a posted fact or a released rule.

### Service completion recognizes revenue

Recognition is accrual-based at **verified service completion**, not charge
issue and not collection. Completion debits patient/insurer A/R and credits
service revenue for the issued gross amount. Explicit discounts and payer
adjustments debit their contra-revenue accounts and credit A/R, leaving:

```text
net_minor = gross_minor - discount_minor - payer_adjustment_minor
```

The existing invoice contract requires positive BRL terms; the net amount must
also remain positive. Issuing a charge alone makes no journal entry.
Appointment completion joins the real clinician transition and the posting in
the same transaction. With an operational book, issued appointment charges
without prepared terms receive explicit zero-adjustment, patient-payer terms;
no practitioner percentage or fixed fee is guessed. Charges with negotiated
terms must freeze those terms before collection/completion.

For a service without an appointment, Finance must attest an opaque reference
to independently retained completion evidence. A final SOAP document, an
invoice or a synthetic adapter result is not silently treated as completion.

Money received before completion remains a liability, not revenue. Allocation
earmarks patient advances; completion debits those advances and credits A/R.
Approved refunds of prepayments are excluded from the amount applied.

### Verified net collection earns practitioner repasse

Practitioner compensation accrues only when verified, posted money is
allocated to the service. Its base is **net collection after discounts and
payer adjustments, before PSP/card fees**. The clinic bears those fees:
debit PSP fees, credit bank or PSP clearing. A fee never reduces repasse.

Each immutable per-service rule is either basis points or fixed centavos.
It names a practitioner and optionally a service type. Invoice terms name the
exact rule version; absence of a rule means no contracted entitlement was
provided, not an inferred percentage. Collection for a former practitioner can
still accrue an existing contracted debt.

Let `C` be cumulative allocated verified centavos less confirmed allocated
returns, `N` the service's frozen net centavos, `B` the basis-point rate, and `F`
a fixed per-service fee. `E` is signed repasse already posted, less signed
confirmed linked reversals:

```text
percentage_entitlement(C) = half_even(C * B / 10000)
fixed_entitlement(C)      = half_even(C * F / N)
new_allocation_repasse    = entitlement(C_after) - E
```

Percentage rules range from zero to 10000 basis points. Fixed fees cannot
exceed net service terms. Cumulative arithmetic prevents payment splitting
from creating rounding gains. A positive entitlement debits compensation
and credits practitioner payables with the practitioner's dimension.
A negative delta is an explicit linked debit correction to payables, not a
negative amount or an omitted rounding cent. Allocation metadata stores its
positive centavo magnitude and `repasse_side`. Refunds invert that recorded
side proportionally as well. This makes the full lifecycle exact even when
several refunded installments originally earned zero rounded cents.

### Refunds and chargebacks reverse the linked entitlement

A request reserves collection capacity immediately, before approval or
dispatch. Requested, approved, sending, unknown and confirmed refunds all
consume capacity; only an authoritatively rejected outcome releases it.
An allocation cannot also spend an unallocated refund's reservation.

Finance approval plus persisted fresh step-up establishes refunds payable.
Its debit is contra-revenue for completed care, patient advances for an
allocated prepayment, or the original liability for unallocated money.
Confirmation debits refunds payable and credits the original money account.
A rejected send has a linked reversal of its approval entry. If service
completion happened meanwhile, restored advances are applied to A/R.
Confirmed returns of advances release that invoice's allocation capacity:
the resulting completion receivable can be collected again. Completed-care
contra-revenue refunds do not reopen allocation capacity. Receipt capacity
itself is never recycled: replacement money needs independent evidence and
a new settlement. Historical legacy `paid` labels are not net A/R; the Finance
projection reads the journal rather than reopening a legacy charge.

At confirmed refund/chargeback, reverse the original allocation's entitlement
proportionally. With original allocation `A`, repasse `P`, cumulative confirmed
returns `R`, and already reversed repasse `V`:

```text
new_repasse_reversal = half_even(P * R / A) - V
```

For a credit entitlement the reversal debits practitioner payables and credits
compensation; a debit correction reverses the other way. Each links to its
original allocation entry. Several partial returns reverse exactly the
full entitlement when the full allocation is returned. Requested, rejected
and unknown outcomes do not fabricate a confirmed entitlement reversal.
If a practitioner was already paid, the resulting debit balance is a visible
clawback against later entitlements, not an automatic bank debit.

### Payouts, approval and unknown outcomes

Only the current exact-clinic `refund.approve` / `payout.approve` permissions
approve outgoing money. Owner/manager legacy billing authority is not approval
authority. Proof is the current session hash resolved against persisted
verification, its real confirmed device, actor, expiry and revocation; no
caller Boolean or timestamp supplies authority.

Payout requests reserve only earned, unreserved practitioner debt. Approval
rechecks entitlement and moves the payable into payout in transit.
Confirmation clears transit against bank. Unknown outcomes retain transit
and the economic reservation; they never authorize another send.

Each dispatch revalidates the executor's current Finance authority and fresh
persisted verification. The original approval and its session binding remain
historical evidence; an expired/revoked approval session cannot authorize a
send by itself. A newly verified Finance executor can act on the same frozen
instruction without rewriting that approval or stranding an earned debt.
Currently authorized Finance may also reconcile retained outcome evidence
after dispatch if an original approver was offboarded: revocation must stop
unauthorized new sends, not erase already observed money.
Every transition has its registered metadata-only audit event.

There is **no real PSP send in this task**. Sending/outcome services are local
synthetic rehearsals and refuse non-synthetic or missing data mode. A confirmed
rehearsal state is not a real-provider receipt. PSP, sandbox and live-money
legs remain **BLOCKED-ON-EG**; no provider flag or activation record is changed.

### Cash close and initial balances

The policy owner can record an opaque evidenced opening asset balance before
that account's first use. This is a separate, idempotent fact against opening
balances; it is never a plug manufactured by cash close. The narrow opening
writer permits an initial petty-cash entry before the first drawer exists.
Ordinary petty-cash postings require an open drawer.

Opening and closing counts must equal posted petty-cash balances. Unposted
entries, unresolved settlement verification, unidentified receipts, suspense
or unknown outgoing outcomes prevent close. Suspense postings require both
a scoped accountable owner and an aging date. Close checks each owner/date
bucket; unrelated positive and negative items cannot hide behind a zero
account total. Linked reversals can clear a historical owner's item after
offboarding without assigning that owner new work. Reopening requires Finance
authority, fresh persisted step-up, the reviewed revision, an idempotent command and a
closed reason (`correction` or `reconciliation`). Every count and reopening
remains in immutable `CashSessionEvent` history.

## Storage, concurrency and compatibility

- Centavos are positive bigint values plus an explicit debit/credit side.
  Reductions and rules are integer values too; no float or Decimal is stored.
  PostgreSQL `numeric` is used only as an unbounded arithmetic intermediate.
- Exact decimal-text reais input rounds half-even once. `10.005` becomes
  `1000`, and `10.015` becomes `1002`. Internal amounts are never re-rounded.
- Largest remainder allocates integer totals using exact remainders, with
  lexical stable-id priority for ties; input order never determines a cent.
  The new corpus is generated from 101 percentage points, four amount
  boundaries, three weight shapes and two input orders.
- Journal balance is a `DEFERRABLE INITIALLY DEFERRED` constraint trigger on
  headers and lines. An unbalanced call may return inside a transaction but
  cannot COMMIT. Posted headers and lines reject edits and deletion; financial
  tables also reject truncation. Corrections retain reversal links.
- `(book, source_kind, source_id)` identifies the economic fact. Replays
  compare canonical line content and reversal identity. Transport delivery ids
  must resolve to the stored settlement fact, not mint new economic facts.
- Clinic, audit-chain and book locks precede child writes, including legacy
  full settlements and reconciliation callbacks before their invoice locks.
  The scope-only lock primitive grants no financial mutation authority.
  A book serializes allocation totals, refund reservations, payable reservations
  and cash counts.
  Services use the same ordering; deferred checks observe final transaction
  state. FORCE RLS and the task-5 least-privilege posture remain independent.
- `Settlement` and `Receipt` keep their historical one-to-one links.
  `JournalSettlement` and `SettlementAllocation` add many-to-many lineage.
  No historical invoice or receipt is rewritten by the migration.
  New trusted full manual settlements can bridge additively when a book exists.
  Full legacy receipts allocate only the remaining net debt; any excess stays
  in the payer's advance/overpayment liability, never a second recognition.
  Existing historical rows are not silently imported.
- `create_invoice` keeps its exact signature. The current Finance bundle gains
  the existing exact-clinic charge boundary; it does not gain patient clinical
  data. Operational journals exclude clinician-only and patient identities.
- The chart has no SaaS subscription account. Clinic Ops subscription revenue
  remains outside clinic books.

Migration rollback after financial facts exist is **restore**, not deletion
of accounting history. Runtime migration, posture, role, concurrency, recovery
and browser acceptance remain required before a gate may claim L1 evidence.

## Accountant formats

`export_journal(*, clinic_id, start, end, output_format)` selects an inclusive
clinic-local date window of at most 366 days from posted journal lines.
Exports require Finance's current `settlement.post` authority and are audited.
The native download is POST-only and never cached.

UTF-8 CSV has these columns, in order:

```text
entry_id,posted_at,account,side,amount_minor,currency,source_kind,source_id,
reversal_of,practitioner_id,owner_id,due_on
```

`amount_minor` is a base-ten integer, never reais or a signed amount; `side`
supplies direction. Times are ISO timestamps, dates are ISO civil dates, and
empty optional dimensions are empty fields.

The OFX-like **custom journal XML**, not bank OFX or an NFS-e, has root
`<journal version="clinic-journal-v1" currency="BRL">`, groups lines under
`<entry id="...">`, and uses one child element per CSV column in each `<line>`.
Both formats contain the same source lines. Neither contains patient names,
clinical text, bank credentials or provider tokens.

## Alternatives rejected and review assumptions

- Charge-issue recognition and cash-basis recognition: neither matches the
  supplied service-completion accrual decision.
- Gross-invoice or post-fee practitioner bases: neither matches net verified
  collection with clinic-borne fees.
- Per-payment rounded fees and per-refund independently rounded entitlement:
  splitting could change the cumulative result.
- Editing settled history, turning a legacy one-to-one field into a new
  relationship, or releasing unknown reservations: each loses economic lineage
  or admits duplicate effects.

This is an operational product policy, not a fiscal/accounting professional's
approval. EG-9 review and provider gates remain external. The missing U1
document and timed-out owner question are provenance gaps, not approvals.
