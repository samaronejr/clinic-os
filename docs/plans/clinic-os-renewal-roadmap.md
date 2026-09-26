# Clinic OS renewal roadmap

Superseded in part by docs/plans/clinic-ops-premium-successor.md (SD ledger).

This page is the human-readable map of the renewal program. The executable
plan `.omo/plans/clinic-os-renewal.md` is canonical: it owns task text,
dependency edges, QA commands and acceptance criteria. This page distills it
for readers. If the two disagree, the plan wins.

Status as of 2026-09-23 (wave statuses mirror the plan checkboxes; a checked
task is complete, an unchecked task is not — including tasks whose synthetic
slice is delivered but whose provider/live slice stays `waiting_external`).
Baseline commit `d70db17cbc5a4efe9c4fe16b6cfe74788b60147e`, detached worktree,
no commits authorized by the plan.

## Where the product stands

Phase 1A is the working baseline. It ships organization/clinic identity, staff
roles through `UserClinicRole`, TOTP and step-up verification, transaction
scoped tenant isolation with fail-closed RLS, an append-only audit ledger,
patient registration and body-only search, practitioner availability, booking,
day/week agendas, rescheduling and terminal cancellation. All of it runs on
synthetic data only, behind the unapproved
[live-data gate](../compliance/LIVE-DATA-GATE.md).

On top of that baseline the renewal domains are now implemented and verified
in synthetic mode: patient contacts, invitations and sessions, versioned
questionnaires, self-booking, waitlist and reminders, encounters with SOAP and
specialty templates, problems/allergies, quarantined attachments, amendments,
retention policies and legal holds, versioned consent, teleconsultation
sessions and screens, physician verification, prescription drafts, PDF/QR
documents, signing and public verification, invoices, PIX, reconciliation,
billing screens and bounded clinic configuration. Every provider-backed slice
(video, e-mail/SMS/WhatsApp delivery, physician registry, PDF rendering,
qualified signing, PIX, attachment storage/scanning, hosted PITR, secrets/KMS)
runs on a clearly labelled synthetic adapter and stays `waiting_external`
until its capability record gains an approved provider and sandbox.

Three service entrypoints remain deferred stubs that raise exactly
`NotImplementedError("Phase >=1")`: `prescription.issue_prescription` (real
issuance waits on the signing capability), `retention.apply_retention_policy`
(automatic destruction stays disabled pending an approved disposal policy)
and `interop.exchange_clinical_record` (RNDS/SNCR preparation only). Their
presence in the tree means the boundary is reserved, nothing more.

## What the renewal adds

The renewal completes the original Wave 1 product on the existing Django 5.2,
PostgreSQL 16, server-rendered template and HTMX stack: patient access,
questionnaire intake, self-booking, waitlist, email/SMS/WhatsApp reminders,
clinical encounters and longitudinal records, attachments, amendments,
retention and legal holds, versioned consent, teleconsultation, physician
verification, signed non-controlled prescriptions with PDF/QR, PIX billing and
receipts, bounded clinic configuration, verified recovery and a staged
design-partner pilot. Product copy is pt-BR; technical documentation stays in
English.

Still excluded: controlled prescriptions and notifications, full SNCR/RNDS
integration, TISS/insurance, inventory, marketplace, native apps, multilingual
records, advanced BI, microservices and Kubernetes. Automatic recording or
transcription of consultations, automated clinical recommendations, patient
merging by demographics and fake signature verification are forbidden, not
deferred.

## Wave map

| Wave | Outcome | Tasks | Status |
| --- | --- | --- | --- |
| A | Renewal verification, current-source builds, real browser route, capability contracts | 1-6, 48-50 | Complete |
| B | Design system, pt-BR, workspace shell, staff intake and scheduling UI | 7-12 | Complete (synthetic, uncommitted) |
| C | Patient access, contacts, invitations, questionnaires, self-booking, waitlist, reminders | 13-19 | 13-18 complete; 19 synthetic slice delivered, real channels waiting_external |
| D | Clinical contract, encounters, problems/allergies, attachments, amendments, retention | 20-25 | 20-22, 24-25 complete; 23 synthetic slice delivered, approved storage/scanning waiting_external |
| E | Consent and teleconsultation | 26-30 | 26-27 complete; 28-30 synthetic slices delivered, real provider waiting_external |
| F | Physician verification, prescriptions, PDF/QR, signing, verification, prescribing UI | 31-36 | 32 complete; 31, 33-36 synthetic slices delivered, registry/rendering/signing waiting_external |
| G | Invoices, PIX, reconciliation, billing UI, clinic configuration | 37-41 | 37, 40, 41 implemented awaiting plan acceptance; 38-39 synthetic slices delivered, real PIX waiting_external |
| H | Cross-domain gates, recovery, release evidence, synthetic rehearsal, live transition, pilot | 42-47 | 42-45 implemented awaiting plan acceptance (45 = synthetic-accepted); 43 provider PITR, 46 live transition and 47 pilot waiting_external |
| Final | Plan compliance, code quality, manual QA, scope fidelity | F1-F4 | In progress; F4 rejected once and fixed, awaiting re-review |

Wave A delivered the machinery every later wave uses: a task-owned evidence
and database lifecycle (task 1), runtime paths independent of the agent
runtime (task 2), a current-source snapshot and build contract that verifies
uncommitted work (task 3), the `renewal_runner` browser and `ci` route (task
4), this document (task 5) and the integration capability register (task 6).
Tasks 48-50 are discovered repairs folded back into wave A.

## Dependency shape

The plan's dependency matrix is the authority. In short: wave B needs the
runner and this roadmap; wave C needs the shared job/callback boundary (task
13) plus capability records; wave D needs patient access and the clinical
contract (task 20); wave E needs consent and encounters; wave F needs
verification, documents and signing; wave G needs the integration boundary
and encounters; wave H needs everything before it. Provider-gated tasks can
build their synthetic slice early, but real integration waits on the matching
capability record.

## External prerequisites

[docs/integrations/capabilities.md](../integrations/capabilities.md) is the
versioned register. Record set `2026-09-12-v1` documents all 16 capabilities
(email, SMS, WhatsApp, video, physician registration, PDF rendering, qualified
signing, signature verification, PIX, attachment storage, attachment scanning,
hosted PITR, data at rest, tenant key management, managed secrets, TLS
transport) as **unavailable**: no provider, package, sandbox or accountable
owner has been approved. A missing capability blocks only its dependent
provider or live action; the synthetic adapter slice proceeds, clearly marked,
and can never be reported as integration complete.

## Synthetic design-partner rehearsal (task 45)

`B end-to-end` (`tests/renewal/browser/test_end_to_end.py`) runs one clinic day
through the real runtime pages as `clinic_app`: an administrator publishes the
specialty template, teleconsult consent text and pre-consultation
questionnaire; reception sets availability, registers, books, verifies an
e-mail contact and issues the patient invitation; the physician assigns the
questionnaire from the agenda, reads the answers, writes and finalizes SOAP
notes, runs the video session and signs a non-controlled document that is
publicly verified, released, delivered and downloaded; reception charges the
visit by PIX and issues the receipt; physician and patient export the record.
It runs at 1280 px and 375 px (plus 320 px reflow, forced colors, reduced
motion) and a 768 px recovery day that loses the reply after the server
committed at booking, note save and payment, drops the video connection,
double-submits and fails the signature, then revokes the patient session and
tries wrong-clinic staff sessions. Every recovery converges on one record.

Adapter labels (also written into each run report):

| Adapter | In the rehearsal | Real provider |
| --- | --- | --- |
| Runtime, RLS, CSRF, TOTP/step-up, outbox worker, record export | real | n/a |
| PDF rendering | `synthetic-pdf-v1` in-process renderer, digest-pinned | waiting_external |
| Tenant KEK / secret store | `synthetic-file` backend, disposable per run | waiting_external |
| Video room and media | synthetic room reference; Chromium fake devices; screens say no media is transmitted | waiting_external |
| Physician registry | synthetic registry, owner-provisioned `SYNTHETIC-` profile | waiting_external |
| Signature provider | synthetic HMAC callback posted by the harness; result "sem validade" | waiting_external |
| E-mail delivery | synthetic channel through the real outbox | waiting_external |
| PIX | synthetic non-payable code; settlement attested on the staff form | waiting_external |

No real sandbox adapter exists: the capability register approves none.
Synthetic acceptance is therefore separate from, and never a substitute for,
the unresolved external tasks: provider selection and sandboxes for every
`waiting_external` row, hosted PITR and encrypted provider backup (task 43),
accountable release approvals (task 44), the live-mode transition (task 46)
and the observed design-partner pilot (task 47).

## Completion states

`synthetic-accepted` (task 45) proves the full journey with labeled synthetic
adapters. It is not live readiness. `live-ready` additionally requires every
capability approval, real provider evidence, the task-46 activation path and
the live-data gate. `pilot-accepted` requires task 47 and F1-F4. Missing
external inputs stay `waiting_external` with the exact unmet prerequisite
named. A sandbox response, mock, browser printout or consent checkbox is never
an approval record.

## Verification commands

`P` expands to `uv run --frozen --no-sync --no-env-file pytest --reuse-db -q`.
`B <suite>` expands to
`uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner browser --suite <suite>`.
Both run in the task-owned environment; prerequisites and failure behavior are
in [RUNBOOK.md](../RUNBOOK.md#renewal-verification-runner). `make ci` remains
the committed-source gate; the runner's `ci` subcommand is the current-source
route for uncommitted work. Every task's stated happy and failure assertions
must really execute; zero collected tests is a failure, not a pass.

## Scope traceability

Each row of the plan's original-scope mapping, its owning tasks and the proof
it must produce. The status column is a snapshot of this checkout, not the
plan's own tracking.

| Original requirement | Owning tasks | Required proof | Status |
| --- | --- | --- | --- |
| Modular monolith, RLS/RBAC/TOTP/audit | 1-4, 13, 20, 42 | Existing invariants plus new-principal and cross-domain denial tests | Foundation shipped; tasks 1-4, 13, 20 complete; 42 cross-domain gates run (findings recorded, release-blocking) |
| Impeccable/taste frontend, pt-BR, PWA | 7-12, 45 | Real component/journey captures, keyboard/reflow and no clinical cache | Delivered in synthetic mode; task-45 rehearsal synthetic-accepted |
| Patient registration and questionnaire intake | 10, 14-16 | Scoped registration/contact/invitation/form journey with versioned responses | Delivered in synthetic mode |
| Staff and patient scheduling | 11-12, 17 | Availability, booking, day/week, reschedule/cancel and concurrent-slot tests | Delivered in synthetic mode |
| Waitlist and all three reminder channels | 18-19 | Expiring offer acceptance; independent email/SMS/WhatsApp contracts and receipts | Waitlist delivered; reminder channels verified on synthetic adapters — real delivery waiting_external |
| Encounters/SOAP/specialty templates | 20-21 | Assigned-clinician encounter, draft recovery and exact template version | Delivered in synthetic mode |
| Problems/allergies/attachments/amendments | 22-24 | Provenance, quarantined authorized files and immutable revision history | Delivered in synthetic mode; approved storage/scanning capability waiting_external |
| Retention/legal holds and record release | 25, 43-44 | Held-record denial, scoped digest-verified export and reviewed policy | Holds/export delivered in synthetic mode; provider PITR and accountable approvals waiting_external |
| Consent and clinic consent overlays | 26, 41 | Exact accepted text/version, revocation and immutable prior receipts | Delivered in synthetic mode |
| Teleconsultation and documentation | 27-30 | Two-persona real-surface journey, provider recovery and preserved notes | Synthetic journey delivered; real provider sandbox waiting_external |
| Physician verification | 6, 31 | Scoped current professional evidence and fail-closed unavailable status | Synthetic registry slice delivered; approved registry waiting_external |
| Non-controlled prescriptions, PDF/QR/signatures | 32-36 | Exact-byte review/sign/verify, rejected invalid signature and private download | Synthetic lifecycle delivered (`synthetic-pdf-v1`, synthetic HMAC signature); real rendering/signing waiting_external |
| PIX and receipts | 37-40 | Exact amounts, authenticated reconciliation and one receipt per settlement | Delivered on `synthetic-pix-v1`; real PIX provider waiting_external |
| Clinic configuration/branding/templates | 41 | Cross-clinic isolation and versioned bounded overlays | Delivered in synthetic mode |
| Recovery, minimum compliance and staged pilot | 42-47, F1-F4 | Full gates, real provider PITR, accountable evidence and observed pilot | Gates and readiness validator run; provider PITR, live transition and pilot waiting_external; F1-F4 in progress |
| RNDS/SNCR preparation only | 20, 32, 44 | Stable internal IDs/versioned documents and recorded future requirements; interop remains a tested stub | Interop stub retained; stable IDs/versioned documents delivered; future requirements recorded |

## Preserved material

- `docs/plans/clinic-os-phase1a-approved.md` and its `.sha256` sidecar are
  frozen CI inputs (SHA-256
  `1cb6d1702dc8960b7bccced4c69a0193418d22d29a521498b083eb6c891dff19`). The
  renewal supersedes planning direction, not these bytes.
- `initial_plan_en.md` and `initialreport.md` are untracked historical
  sources. Their regulatory, vendor, pricing and delivery assumptions need
  revalidation before reuse.
- `.omo/evidence/` is shared across worktrees and retains the Phase 1A
  isolation ledger and execution-host proofs. The 2026-09-12 cleanup archived
  120 inert files outside the checkout; see
  [the cleanup record](../maintenance/2026-09-12-omo-cleanup.md). Do not
  re-perform it.
- Current design lives in [DESIGN.md](../../DESIGN.md); domain notes in
  [apps/intake/README.md](../../apps/intake/README.md) and
  [apps/scheduling/README.md](../../apps/scheduling/README.md); trust
  boundaries in [SECURITY.md](../SECURITY.md); module status in
  [ARCHITECTURE.md](../ARCHITECTURE.md).
