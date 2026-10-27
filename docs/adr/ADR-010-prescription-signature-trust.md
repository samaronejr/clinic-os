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

## Todo 35 selected cryptographic mechanism and anchor policy

The selected mechanism is local verification with **cryptography 50.0.2**,
already locked in `uv.lock`. No new dependency is added. The earlier recorded
verdict implementation was rejected by T35-VERIFY: invented positive strings
could accept unsigned bytes. It is replaced, not treated as trust evidence.
`SignatureVerificationAPIAdapter` retains its public seam but makes every
verification decision locally from signatures, certificates and signed CRLs.

The sandbox profile `clinic-synthetic-x509-v1` preserves the exact PDF bytes
as the envelope prefix. RSA-PSS/SHA-256 with a 32-byte salt signs a
domain-separated RFC 8785 binding plus those exact bytes. The binding includes
operation reference, rendered digest, signer, class and approval digest.
A distinct timestamp certificate signs the document-signature SHA-256 imprint
and aware time under a different domain. Verification reconstructs both signed
messages and checks their signatures, rather than trusting a claimed digest,
time or signer verdict. This is a local synthetic timestamp proof, not an
RFC 3161 or PAdES interoperability claim.

The configured trust anchor is an explicit DER root certificate, never a root
supplied by the signed envelope. The sandbox root is self-signed and has a
synthetic subject. The fixed-depth path has one intermediate (path length 0)
and separate document/timestamp leaves. The verifier checks actual issuer
signatures, authority/subject key identifiers, validity at authenticated signing
time, CA/basic constraints, key usage, critical extensions and RSA strength.
The signing leaf's single URI identity must equal the approved signer.
Its purpose is the synthetic document OID `1.3.6.1.4.1.55555.35.1`; the timestamp
leaf requires the timestamp identity and critical time-stamping EKU.

The existing `VerificationAPI.verify(bytes)` transport now returns only a
closed pair of DER CRLs, not chain/timestamp/revocation assertions. Each CRL
must have the matching issuer, a valid issuer signature, matching authority
key identifier and a current update window. Root and intermediate CRLs cover
the intermediate, signing leaf and timestamp leaf. Any listed serial is
rejected conservatively; there is no historical-revocation exception.
A missing or stale revocation source means `reconcile`, never acceptance.
No AIA, OCSP or other network lookup is performed by the local verifier.

`tests/prescription/fixtures/signature-pki.json` contains a once-generated
synthetic root/intermediate, valid/revoked/expired signing leaves, a timestamp
leaf, wrong root and broken intermediate, signed CRLs and fixed signed vectors.
Its generation script independently constructs the vectors with fixed dates.
Only explicitly public test leaf/timestamp private keys are retained; CA
private keys are not emitted. None is a production credential or trust anchor.
The implementation uses cryptography's X.509 and RSA verification APIs:
https://cryptography.io/en/latest/x509/reference/
https://cryptography.io/en/latest/hazmat/primitives/asymmetric/rsa/

Only the explicit synthetic anchor/profile is runnable. A live trust anchor,
ITI-compatible production profile, actual cloud-signing partner and PAdES
interoperability remain BLOCKED-ON-EG-1/EG-5 and require recorded approval.
Selecting this local mechanism does not clear an EG or enable live mode.

The prepare/sign/status/verify protocol sits beside the original callback
contract. The original synthetic provider remains the default. The recorded
adapter first commits encrypted candidate bytes as `signed_unverified`.
Timeout means reconcile those same immutable bytes, not signing again or
assuming failure. A worker rechecks current authority and exact-byte approval
after cryptographic verification and reaches only `rehearsal_complete`. It never reaches
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
