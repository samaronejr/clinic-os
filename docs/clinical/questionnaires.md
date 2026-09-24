# Versioned pre-consultation questionnaires

Task 16 delivers synthetic questionnaire configuration, not medical advice.
Only synthetic data is authorized. The protected-data-v1 encryption inventory
and live-data gate remain binding; this task does not implement envelope
cryptography or authorize real patient data.

## Configuration and assignment

`publish_template` accepts an exact clinic owner/admin context and creates a
new immutable row for each `(organization, clinic, key, version)`. Version
allocation is serialized by a transaction advisory lock and a unique constraint.
Existing templates cannot be updated or deleted by the runtime role; database
triggers also reject historical mutation. Publishing never changes a response
already assigned to an earlier version.

The schema is closed: 1-40 questions; distinct `q_` identifiers with 1-32 lowercase
letters/digits/underscores; labels 1-200 characters; types `text`, `selection`,
`boolean`; a real boolean `required` flag; `max_length` 1-4000; and `options`.
Text/boolean use an empty options list. Selection uses 1-30 distinct nonblank
strings, each no longer than 200 or its declared limit. Title limit: 160;
configuration key: 64. Unknown keys, expressions, scripts, conditions, calculated
questions and executable content are rejected. All labels and answers are
HTML-escaped. Boolean values are typed booleans, not strings or integers;
`false` counts as a supplied answer. Length limits apply to textual values.

`assign_questionnaire` requires a physician assigned to the exact clinic and
binds the response to its patient, enrollment and immutable template FK.
An optional appointment must belong to that same patient/clinic/organization.
A database guard checks all bindings and prevents their subsequent mutation.
Clinic owners/admins publish versions from the clinic settings screen
(`/clinics/<clinic>/settings/`): a key, a title and up to five fixed question
rows (label, type, required flag, optional limit, options one per line). The
rows are turned into the closed schema above and re-validated by
`publish_template`; there is no free-form schema input or medical template
generator. Physicians assign the newest version of a configuration from their
own agenda row ("Questionários"): the enrollment is resolved server-side from
the appointment, and a repeated assignment of the same version to the same
appointment returns the first response instead of creating another.

## Draft, submit, reopen

- Assignment creates revision 1, empty answers, state `draft`.
- Save draft accepts missing required answers, but validates every supplied
  value's type, choices, keys and length. It increments the revision atomically.
- Explicit submission requires every required answer; required text cannot be
  whitespace-only. Submission records `submitted_at` and freezes patient edits.
- Every write locks the response and compares the expected revision; stale tabs
  cannot overwrite a later save or submission. Invalid input changes nothing.
- Only an assigned clinic physician can reopen the current submitted revision,
  with a nonblank reason up to 255 characters. Reopening keeps answers and exact
  template version, clears the current submission timestamp and increments the
  revision. The patient can then correct and explicitly resubmit.
- Database-written append-only `QuestionnaireEvent` receipts capture assignment,
  saves, submissions and reopens, including revision, actor or patient session,
  timestamp, reason and exact answers. Old submissions remain intact on reopen.
  These receipts contain sensitive data and share physician-only read policies;
  answers are not placed in general audit metadata, URLs or application logs.

## Authority and surfaces

New invitations grant `enrollment_view`, `questionnaires` and `booking`. Existing grants and
sessions are not silently elevated: clinic staff must issue a fresh invitation
if an older session lacks the operation. Questionnaire RLS revalidates the live
patient session, grant revocation, both expiry deadlines and allowed operation.
The patient request never gets staff tenant or actor GUCs.

`/patient/questionnaires/` is linked from the patient home. Response identity,
answers and optimistic revision travel only in POST bodies with CSRF protection.
Forms work natively without JavaScript. The same no-store middleware used by the
patient portal covers these pages.

`/intake/clinics/<clinic>/questionnaires/` is the staff surface, linked by a
POST-body action from staff patient search, with the existing privileged-role
TOTP guard. Reception/owner/admin can retrieve completion state
and revision using the narrow status resolver, never answers or answer history.
Physicians assigned to that exact clinic can inspect and reopen. Being an owner
or administrator alone does not confer clinical access. Patient and staff roles
are independently checked in services and PostgreSQL policies; swapped response
IDs fail non-enumeratingly. No broad organization-only policy exposes sensitive
response or receipt rows to reception.

## Verification

`P tests/renewal/test_questionnaires.py tests/test_module_boundaries.py && B questionnaires`
uses the renewal plan's P/B commands. The browser suite runs as `clinic_app` with
real invitations, CSRF, patient sessions and physician TOTP. Owner access is
confined to synthetic fixture/configuration setup. Screenshots contain synthetic
content only, with no trace recording. The historical interop and other deferred
service sentinels remain intact; only the intake submission stub is replaced.
