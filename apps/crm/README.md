# Ethical CRM

This is a synthetic implementation. All real messaging providers and sandbox
legs remain **BLOCKED-ON-EG**. No provider selection, lawful-basis review or live
approval is claimed. Existing comms adapters are the only transport and return
synthetic acceptance, never proof of delivery.

Only the exact-clinic `clinic_manager` bundle with current `demographics.read`
may capture leads, manage sources, publish factual booking copy and run campaigns.
Python and FORCE RLS independently check the same scope. Unknown and foreign
records share one denial. No new identity migration or permission broadening is
needed.

`create_lead` retains name and email in purpose-bound envelopes, separate from
patients, with a ninety-day deadline. It grants no outreach permission. An
explicit `convert_lead` maps an independently verified enrollment; a contact is
never identity proof. `redact_expired_leads` removes contact and enrollment, keeps
the minimal nonclinical receipt, and runs whenever the manager opens CRM.
`conversion_report` reports retained-cohort counts and their denominator, not
clinical dimensions or inferred lifetime conversion. Sources are administrative
only; source labels are encrypted.

The native conversion form offers up to 200 recent active prospects and scoped
patient enrollment choices with demographic labels. It requires an explicit
selection and identity check; operators do not have to discover or type opaque
record identifiers. Selection values stay in POST bodies.
Published clinic copy can be updated through the same native form; its booking
code remains stable. Repeating an unchanged publication returns the same receipt
without another audit append, and duplicate source codes return a conflict.

Campaigns use draft -> approved -> sending -> sent with optimistic revisions.
They support a bounded cohort of at most 200 enrollments and fail closed above
that bound, rather than silently sampling. Each recipient needs BOTH current,
unrevoked marketing purpose consent and their own current channel opt-in bound
to the verified destination version. Changing the destination invalidates that
choice. Missing choices, newer consent text, refusal or revocation excludes the
recipient. The preview shows eligible and excluded counts without identities.
Clinical-condition targeting is blocked: no reviewed lawful-basis record exists,
and no condition filter or clinical join is implemented.

The only campaign templates are the exact neutral copy in `rules.py`, with no
variables. Diagnosis placeholders and all other substitutions are rejected.
Registering the consent source also gates ordinary marketing inbox messages;
their command, DB insert guard and send-time recheck enforce the neutral rule.
The adapter rebuilds marketing bytes from that neutral template and uses no
stored parameters, so even an independently inserted encrypted message body
cannot become an outgoing clinical message.
Campaign receipts point to comms marketing messages and contain no body, contact
or clinical content. A sending batch becomes sent only once every queued message
has successful outbox receipts; failed/cancelled batches remain visibly sending.
Sent means provider acceptance, not delivery. A deterministic command key per
campaign and recipient prevents duplicate queuing.

The stable `crm.marketing:<clinic>` send boundary is shared by purpose publication,
revocation, patient channel choices, destination mutations and marketing thread
mutations. A revocation that commits before a provider call wins the final
recheck; an in-flight call completes before revocation commits. Both SQL triggers
and the outbox use this boundary, including conversations created after a choice.

Patients use `/patient/outreach/` for equally prominent allow/refuse actions, and
`/patient/consent/` for the separate exact text. No choice is preselected and
neither care nor booking depends on optional consent. Delegates cannot set
marketing choices. `/patient/feedback/` records one voluntary score from zero to
ten for the patient's own completed visit. There are no free-text clinical
answers or diagnosis fields. Scores and choices append patient-session audit
receipts; managers see aggregate score counts only on the CRM page.

`/book/` has one fixed URL. Selecting a clinic code, service, time or booking
token happens only in POST bodies. The clinic code is saved server-side.
`crm_public_profile` projects published clinic name and factual introduction
only. An anonymous user must verify an existing private patient invitation at
`/book/access/`, which calls the existing invitation redemption service without
putting a clinic selector into its URL. Booking then reuses `patient_session_context`,
`patient_services`, `patient_slots` and `book_patient_slot`; staff cookies never
grant patient authority and a session from another clinic is refused.
No prospect is silently registered as a patient. No fictitious services,
testimonials or outcome claims are published.

All seven new tables use FORCE RLS, limited runtime grants, immutable provenance
and no DELETE/TRUNCATE. State updates have column grants and transition triggers.
Migration `0001_initial` adds only new encrypted tables, with no plaintext
cutover. Empty-schema reverse is supported and rehearsed; populated rollback is
restore, because dropping retained receipts is not rollback. Resolver/posture
pins are checked against the live PostgreSQL catalog.

Surfaces are native forms with English msgids and committed pt-BR `.po`/`.mo`.
No patient selectors, clinical text or contacts enter URLs, outbox rows, logs,
browser storage or audit payloads. The new `public-booking` browser suite owns
desktop/mobile, keyboard, reflow, axe, no-JavaScript and selector probes.
