# ADR-011: Integer-centavo double-entry journal

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Threat model: [payments](../threat-models/payments.md)

## Context

Billing today covers invoices, synthetic PIX, reconciliation and receipts, with
a one-to-one link between settlement and invoice. The successor scope adds
partial payments, installments, deposits, refunds, payouts, professional
compensation, insurer receivables and cash close. Money bugs are silent and
compound, so the storage model has to make an unbalanced or double-posted fact
impossible rather than unlikely.

## Decision

The operational journal stores integer centavos in an immutable double-entry
ledger.

- A deferred constraint trigger checks that each entry balances at commit.
- Posted entries are immutable; corrections are linked reversals.
- Rounding is half-even, applied only at versioned policy boundaries.
- Splits use largest-remainder allocation so parts always sum to the whole.
- Each economic fact has a unique posting identity, so it posts once.

## Consequences

- Balance, immutability and single posting are database guarantees, not code
  conventions.
- Exports to accountants come from the journal (CSV and a documented journal
  format), not from ad hoc report queries.
- SaaS revenue for Clinic Ops itself stays out of clinic books (todo 64).
- Existing one-to-one settlement rows stay; new allocations use a separate
  table.

## Rejected

- Float or Decimal storage. Floats lose cents, and Decimal columns invite
  mixed scales and rounding in the wrong layer.
- An external ledger SaaS. It moves financial facts outside RLS and adds a
  provider with no contract path yet.

## Revisit trigger

An accountant integration need that the journal export can't serve.

## Owning todos

56. Consumers: 57, 58, 59, 64.
