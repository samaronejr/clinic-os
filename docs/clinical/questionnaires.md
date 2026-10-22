# Versioned pre-consultation questionnaires

Task 16 delivers synthetic questionnaire configuration, not medical advice.
Only synthetic data is authorized. The protected-data-v1 encryption inventory
and live-data gate remain binding; this task does not implement envelope
cryptography or authorize real patient data.

## Configuration and assignment

`publish_template` accepts an exact clinic owner/admin/physician context and creates a
new immutable row for each `(organization, clinic, key, version)`. Version
allocation is serialized by a transaction advisory lock and a unique constraint.
Existing templates cannot be updated or deleted by the runtime role; database
triggers also reject historical mutation. Publishing never changes a response
already assigned to an earlier version.

The schema is closed: 1-40 questions; distinct `q_` identifiers with 1-32 lowercase
letters/digits/underscores; labels 1-200 characters; types `text`, `selection`,
`boolean`, `number`; a real boolean `required` flag; `max_length` 1-4000; and `options`.
Text/boolean/number use an empty options list. Selection uses 1-30 distinct nonblank
strings, each no longer than 200 or its declared limit. Title limit: 160;
configuration key: 64. Unknown keys, expressions, scripts, calculated
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

## Declarative branching (todo 29)

Legacy list definitions remain valid. V2 definitions are closed envelopes:
`{schema_version: 2, state: draft|published|retired, questions: [...], rules: [...]}`.
Publication validates the shipped
`docs/clinical/questionnaire-branching-schema.json`, then checks distinct IDs,
literal types, choice membership and references. Every target must occur after
its source: backward edges, self-references and cycles are refused. Publication
always appends an immutable version; draft and retired versions cannot be
assigned, in services or in the database. Existing assignments stay pinned.

A rule is exactly
`{when: {question: "q_id", op: "eq"|"in"|"gt"|"lt", value: literal}, show: ["q_id"]}`.
`in` takes a nonempty list of scalar literals; every other operator takes one
scalar. Nested values, expression syntax, unknown keys and executable operators
are refused. Boolean, string and number comparisons never coerce. Numbers are
finite binary64 values bounded by 9007199254740991 in magnitude; comparisons
use the same representation in Python and JavaScript. Numeric operators require
a number question. An absent, null or empty-string answer never matches, including
against an empty-string literal. False and zero remain answered values.

Questions not targeted by a rule are visible. Multiple rules targeting one
question are alternatives (OR). Evaluation follows question order, and a hidden
source cannot reveal descendants, even if a submitted body forges its answer.
The server validates supplied types, removes hidden values, then requires only
visible required answers on submission. The plain script enhances the same
native POST form; with JavaScript off, saving the current answers renders the
next visible questions. No answer is filled automatically. Shared vectors in
`tests/fixtures/questionnaire-branching-vectors.json` run in Python and through
Playwright's evaluate against the shipped script on all three engines.

The settings rows expose earlier-question selection, a fixed comparison and
literal values (one per line for `in`). They do not accept expressions or a
free-form schema. A row without a condition remains visible.

Carry-forward is an explicit patient action from a submitted response to an
empty draft of the exact same retained template and enrollment. It copies no
other patient's or clinic's answers. V2 answer envelopes are
`{values: {...}, unverified: ["q_id", ...]}` inside the existing encrypted
column; append-only receipts preserve the flags with the answer bytes. Each
copied answer is labelled unverified. Submission is refused until the patient
explicitly reviews it or changes it; an unchanged form repost is not review.
The existing v1 answer shape is unchanged.

## Specialty template v2

`docs/clinical/template-schema-v2.json` is the server validation authority for
`ehr.services.publish_template`. V1 still requires exactly the four SOAP keys.
V2 retains those four top-level prompts and adds `schema_version: 2`, `state`,
`fields`, `phrases` and `optional_sections`. Template states are immutable
version snapshots, not in-place row mutations. A new note cannot bind a draft
or retired snapshot. Prior documents retain their exact version.

Fields have distinct IDs, a SOAP section, label, required flag and one of text,
number with unit and bounded range, choice, multichoice, date, boolean or a
versioned scale reference. No schema accepts a default answer or executable
content. Scale references are identifiers, not a calculator implementation.
Reusable phrases are explicitly clinic-scoped or bound to the publishing
clinician; another clinician's identity cannot be claimed. Definitions are
configuration only: publication does not copy a phrase, a range or an optional
section into a patient's note and never invents normal findings.

The template-specific trigger replaces only the specialty-template binding
and plain-text triggers; the encounter binding function is unchanged. The
existing `jsonschema` dependency is promoted from dev to runtime so deployed
publication validates the same shipped schema as tests. Neither migration
adds a tenant table or a plaintext answer column. Reversing after a v2
definition exists refuses: rollback is a restore, not deletion of history.

Each staff-created draft, published or retired snapshot appends its registered
metadata audit event in the same transaction. The catalog distinguishes
`ehr.template` and `intake.questionnaire_template` lifecycle events and keeps
only the clinic and object verb in their payloads. Patient response writes
retain their existing encrypted `QuestionnaireEvent` receipt path; they never
set a staff tenant GUC or gain generic audit authority.

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

`P tests/renewal/test_questionnaires.py tests/infra/test_module_boundaries.py && B questionnaires`
uses the renewal plan's P/B commands. The browser suite runs as `clinic_app` with
real invitations, CSRF, patient sessions and physician TOTP. Owner access is
confined to synthetic fixture/configuration setup. Screenshots contain synthetic
content only, with no trace recording. The historical interop and other deferred
service sentinels remain intact; only the intake submission stub is replaced.
