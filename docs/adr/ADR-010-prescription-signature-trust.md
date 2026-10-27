# ADR-010: Prescription class taxonomy and qualified signature trust

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Related decisions: D-21, SD-6

## Context

Prescriptions today are drafts plus `synthetic-pdf-v1` documents with
synthetic signing and a public verification page. A DB check limits the
category to `synthetic_non_controlled`, and `issue_prescription` is a deferred
stub. Real prescriptions differ by class: simple, controle especial,
antimicrobials with retention, Notificacoes A/B/B2, retinoids and
thalidomide. Some classes must go through SNCR on dates set by D-21, and a
valid electronic prescription needs an ICP-Brasil qualified signature.

## Decision

Prescription trust has five parts:

- a class taxonomy with per-class eligibility gates;
- approval of the exact rendered bytes, bound to signer and class;
- an ICP-Brasil qualified signature through a partner;
- independent verification of the signed document (chain, timestamp,
  revocation) instead of trusting the signer's own answer;
- SNCR integration per class where the class requires it.

## Consequences

- Any edit after approval invalidates the approval, because the digest no
  longer matches.
- Classes that need SNCR are refused for electronic issuance until EG-6
  clears, and the product offers the labeled paper path instead.
- The synthetic category stays for rehearsal and never becomes `issued`.
- The verifier library or partner verification API is recorded here when
  todo 35 picks it.

## Todo 35 recorded verification API boundary

The selected local mechanism is `SignatureVerificationAPIAdapter`, separate
from the signing object. It consumes the closed normalized response schema
in `apps/prescription/signature_verification.py`: exact signed/content
digests, operation reference, signer, class and approval digest; positive
chain, timestamp and revocation verdicts; aware signing/check timestamps.
Duplicate/unknown fields, negative or absent verdicts and every binding
mismatch fail closed. Verification runs outside the tenant transaction.

The only supplied API is `RecordedVerificationAPI`. Its immutable response
is supplied by the rehearsal harness for one exact artifact digest.
The fixture declares `synthetic-fixture`, `recorded-rehearsal-v1` and
`synthetic-envelope`; these are not a trust anchor, qualified certificate,
real PAdES profile or ITI validation result. The signer cannot manufacture a
verification receipt, and its legacy envelope verifier is not consulted on
this path. No live library, partner API identity or trust policy is selected.
Those remain BLOCKED-ON-EG-1/EG-5 and require a successor recorded decision.

The prepare/sign/status/verify protocol sits beside the original callback
contract. The original synthetic provider remains the default. The recorded
adapter first commits encrypted candidate bytes as `signed_unverified`.
Timeout means reconcile those same immutable bytes, not signing again or
assuming failure. A worker rechecks current authority and exact-byte approval
after verification and reaches only `rehearsal_complete`. It never reaches
`issued`; all real class/lifecycle/runtime gates remain closed.
The additive migration preserves old signed artifacts and grants; rollback
requires restore because accepted candidate history is immutable.

## Rejected

- One generic signed PDF for every class. It ignores the class-specific rules
  and would let a controlled class look valid when it isn't.

## Revisit trigger

A regulation change affecting prescription classes, signature levels or SNCR
scope.

## Owning todos

35, 36. Consumers: 33, 34.
