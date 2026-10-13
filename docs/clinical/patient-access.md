# Patient access: enrollment-bound invitations and sessions

This document is the binding policy for patient self-service access. It is
written before the implementation it governs; the code must match these
defaults exactly.

## Actors and authority

- **Staff** (owner, clinic admin, receptionist — the manager roles) issue and
  revoke access for one enrollment inside a clinic they are authorized for.
- **Patients** hold no `identity_user` row, no password, no staff role and no
  TOTP device. A patient session never authenticates as a staff user, never
  impersonates one, and never merges with a staff identity.
- The server is the only authority for patient, clinic, organization and the
  allowed operation set. Client-supplied identifiers (patient ids, enrollment
  ids, organization ids, tenant headers) are never accepted as authority; the
  session row is the binding.

## Invitations (`intake_patientaccessgrant`)

- Staff issue an invitation for one **existing enrollment** in their clinic.
- The invitation secret is a random 256-bit URL-safe token generated at issue
  time. Only its **SHA-256 hash** is stored (`secret_hash`, 32 bytes); the raw
  secret is shown to staff exactly once in the issuing response and is never
  written to a URL, a log, or the database.
- **Expiry: 24 hours** from issuance (`expires_at`). **Single-use**: the first
  successful redemption sets `consumed_at`; later attempts fail.
- Staff revocation (`revoked_at`) invalidates the invitation **and** every
  patient session minted from it.
- Redemption is a POST to `/patient/access/<clinic_id>/` carrying the code in
  the request body. The clinic in the path is part of the lookup: a code
  issued for another clinic fails identically to an unknown code.
- Every redemption failure — unknown code, expired, consumed, revoked,
  wrong clinic — returns the same generic message. The endpoint never
  enumerates which invitations exist or why one failed.

## Patient sessions (`intake_patientsession`)

- A successful redemption atomically consumes the invitation and creates one
  session row bound server-side to `organization_id`, `clinic_id`,
  `patient_id`, `enrollment_id` and the granted `operations` set.
- **Idle expiry: 30 minutes** (`idle_expires_at`, renewed on each validated
  request). **Absolute expiry: 8 hours** (`expires_at`, fixed at creation).
- Sessions are revocable: `revoked_at` set by staff revocation or by patient
  sign-out ends the session immediately.
- Validation happens inside the database (`clinic_app.touch_patient_session`),
  which checks revocation and both expiries and renews the idle deadline in
  one statement. The session id travels only in the signed session cookie;
  the runtime role holds no INSERT on the session table, so sessions can only
  be minted by the redemption function.
- Patient requests run in their own transaction boundary with no staff tenant
  GUCs: `app.current_user_id` and `app.current_tenant` stay unset, so every
  tenant-isolation policy fails closed for patient requests. Patient reads go
  through `SECURITY DEFINER` resolvers that re-validate the session row.
  Questionnaire and self-booking policies use the same validated session
  boundary; they do not open the staff organization-level policies.
- Patient sign-out revokes the session server-side and removes it from the
  cookie; it does not touch any staff session sharing the cookie.

## Allowed operations

The granted operation set is stored on the invitation and copied to the
session at redemption. `enrollment_view` permits viewing the invited enrollment
(patient name, clinic name and enrollment date) and signing out. Task 16 adds
`questionnaires` to newly issued invitations: patient-session-specific policies
permit reading and updating only the bound enrollment's assigned forms, with
live session/operation checks. See [questionnaire policy](questionnaires.md).
Existing invitations/sessions are not upgraded; issue a fresh invitation for
new operations. Task 17 adds `booking` to new invitations: patients may choose
30-minute clinic-local slots, book/view their own appointments, reschedule with
the same practitioner and cancel with reason `patient_request`. Shared scheduling
services retain idempotency, availability revalidation, terminal cancellation
and exclusion protection. See [scheduling](../../apps/scheduling/README.md).
Task 25 adds `records` to new invitations: patients may view and export the
document versions released to them, with live session/operation checks and a
stored export receipt. See [record contract](record-contract.md).
Task 26 adds `consent` to new invitations: patients can read exact versioned
texts, explicitly accept and later revoke their own decisions, and inspect retained
receipts. Existing grants are not upgraded. Consent never grants record access or
message preferences. See [consent policy](../../apps/consent/README.md).
A session can never exceed the operations it was minted with.

## Recovery policy

- **Lost or unreceived code**: staff issue a new invitation. Old outstanding
  invitations remain valid until their own expiry, consumption or revocation;
  issuing a new one does not implicitly revoke earlier ones.
- **Expired or consumed code**: not recoverable; staff issue a new
  invitation. There is no renewal of a spent grant.
- **Expired or revoked session**: the patient must redeem a fresh invitation;
  sessions are never resurrected or extended past their absolute expiry.
- **Compromise or error**: staff revoke the grant, which ends the invitation
  and all sessions derived from it in one action.
- There is no patient self-service recovery for the original identity and no identity proofing beyond
  possession of the single-use code; a patient who loses access returns to
  the clinic for a new invitation.

## Guardian and delegate representation

`identity.PatientDelegateGrant` is the sole representation authority store.
A contact, invitation, family relationship or message channel never establishes
legal representation. Staff with `configuration.clinic` or
`configuration.organization`, or the authenticated represented patient, can
request a grant. Independent verifier staff must review its legal authority and
scanned document reference before `pending_verification` becomes `verified`.
Rejected grants are terminal. Only verifier staff or the represented patient
can activate a verified grant or revoke a nonterminal grant.

The authority kind is a legal guardian of a minor, curator, or
patient-authorized adult. The verifier attests that kind from the evidence;
the application does not infer it from contact possession. Both enrollments
and the evidence belong to the same clinic and organization. Purposes,
operations, subjects, evidence and expiry are immutable. A command retry does
not append another transition audit, and stale revisions cannot skip a state.

`PatientSession.enrollment_id` retains the authenticating identity.
`acting_for_enrollment_id` and `delegate_grant_id` bind its selected
representation. The POST-only `/patient/delegation/switch/` accepts a grant
selector in the request body, not a represented patient or tenant claim.
`clinic_app.current_patient_authority` intersects the original invitation
operations with the verified grant, resolves the represented enrollment, and
checks revocation and expiry at every database statement. Booking, intake,
consent, released records, prescribing, teleconsult and billing use that
effective scope. Original patient access and staff roles remain separate.

Confidential releases and questionnaire responses default to
`restricted_from_delegates=True`. An explicit clinical decision is required
when creating a shared release or assigning a shared response; audience
metadata cannot later be silently widened. Restricted versions never appear
in the delegated release resolver or export, including when selected directly.
Consent actions additionally require the exact verified purpose and store
`delegate_explicit_action`; one purpose does not authorize another.
Patient AI-use attestations are not disclosed to delegates.

An expired grant stays bound to the session and denies representation; it does
not silently return to the patient's own scope. The expiry screen offers a
signed, body-only renewal request. This creates a new `pending_verification`
grant with the same subjects, operations, purposes and evidence, and never
resurrects access. The original identity must still be live. Revoked grants
cannot renew. Expiry is checked on use, without a cron dependency or mutable
deadline. Every persisted lifecycle transition and session switch uses
`record_phase1_event` with only the permitted metadata payload keys.

The legal authority and confidentiality policy require EG-2 human review.
These synthetic implementation tests do not approve legal authority for live
patient data.

## Non-goals

- No patient passwords, accounts or cross-clinic identity.
- No staff role assignment, impersonation or identity merging.
- No client-supplied tenant, organization or patient context is trusted.
- Invitation codes and session identifiers never appear in URLs or logs.
