# Interoperability contract - governed data import (todo 66)

Interop owns the governed migration path from spreadsheets and exports into a
clinic: upload, profile, map, validate, dry run, sign-off, batched commit,
reconciliation and rollback. Its public entrypoints are in `services.py`. It
adds no browser surface yet; every command is a keyword-only, clinic-scoped
service checked again by PostgreSQL (FORCE RLS, one guard trigger, resolver
functions). `exchange_clinical_record` (RNDS, todo 68) still raises
`NotImplementedError("Phase >=1")`.

Competitor-specific layouts are out of scope: no mapping is invented for a
named product until owner-supplied EG-15 sample exports exist. EG-15 stays
open.

## Contract files

- `docs/interop/import-mapping-v1.json` - the JSON Schema (Draft 2020-12) for
  mapping documents (`$defs/mapping`) and profile reports (`$defs/profile`).
  `apps/interop/contract.py` holds the same schema; a test requires the two
  to be equal.
- Archive layout `clinic-import-archive-v1`: a ZIP holding exactly
  `manifest.json`, `patients.csv`, optional `history.csv` and flat
  `files/<name>` attachments. The manifest lists every other member with its
  `sha256` and `bytes`; any missing, extra or mismatched member fails the
  upload (`manifest_mismatch` / `checksum_mismatch`).

## Sources and limits

| Kind | Content |
| --- | --- |
| `csv` | one `patients` dataset; UTF-8 (BOM allowed) or Windows-1252; `,` `;` tab or `|` |
| `xlsx` | first worksheet's cached text; formula cells are row-level `formula_cell` rejects; DTDs are refused |
| `api` | `{"datasets": {"patients": [{column: text or null}], "history": [...]}}`, no duplicate keys |
| `zip` | the archive layout above; `history` is allowed only for `zip` and `api` |

Sources are at most 20 MiB, 50,000 rows and 64 columns per dataset; archive
members at most 10 MiB, 64 MiB in total, a 100:1 compression ratio above
64 KiB, no symlinks, no encryption and no paths outside the flat layout
(threat model IE-E2). Nothing is extracted to disk.

## State machine and authority

`ImportJob` is an immutable header; state lives in append-only
`ImportTransition` rows with a dense `sequence` (every command names the
`expected_sequence` it observed; a stale or replayed command is
`interop_stale`):

```
uploaded -> profiled -> mapped -> dry_run -> approved -> committed
                        mapped <- dry_run (re-map)     any non-terminal -> rolled_back
```

| Command | Authority | Notes |
| --- | --- | --- |
| upload, profile, suggest/save/apply mapping, approve, rollback, read job | import: `staff.clinic` (clinic manager, legacy clinic admin) or `staff.organization` (org admin, owner) at the exact clinic | approve signs the cutover/rollback plan |
| dry run, dry-run report and export, commit, batch run | import **and** `demographics.read` | dry runs compare with existing identities, commits write them |
| imported history and documents | patient-scoped `clinical.read` | never the import authority |

Organization admins hold no demographics access by default (RP matrix; the
intake duplicate guard refuses them too), so a clinic manager runs the dry run
and the commit; an org admin may prepare, sign off and roll back. Commit and
every post-approval rollback need this actor's fresh persisted step-up session
(`session_key`), verified in Python and again by the guard through
`clinic_app.identity_session_verified`. Unknown, foreign and unauthorized
selectors share `ImportAccessDeniedError`; refusals carry closed machine
codes (`access.Code`), never source values.

## Validation and dry run

Validators are deterministic: CPF check digits and CNS length (intake's
`normalize_identifier`), dates in the declared format (`DD/MM/YYYY`,
`YYYY-MM-DD` or `excel_serial`; no future birth dates), clinic-local
`DD/MM/YYYY HH:MM` or ISO minutes (never in the future), closed vocabularies
(sex at birth, history kinds), the local observation code set with its exact
UCUM-like unit (`ehr.longitudinal_values`), clinic-required demographics,
in-file duplicate source ids and identifiers, and patient names that would be
spreadsheet formulas (`formula_like`, legal and social names). The dry-run
report lists `[dataset, row, field, code, source_id]` rejections, where
`source_id` is the operator's own row label, and reconciles
`rows = accepted + rejected` per dataset. `export_import_report` returns the
same reasons as CSV in which every cell that a spreadsheet would evaluate
(leading `=`, `+`, `-`, `@`, tab, carriage return or their full-width forms,
also after leading spaces) is prefixed with an apostrophe. Existing-patient matches
(`exact_identifier`, `dob+name_prefix`, compared only through tenant HMAC
indexes of visible patients) are reported per row; a commit still creates the
patient and intake's own duplicate detection opens a `DuplicateCandidate` for
human adjudication (todo 18). Nothing is ever merged automatically.

The dry-run digest (RFC 8785 + SHA-256) binds the source checksum, the
mapping digest, every accepted normalized row and every rejection. Approval
binds that digest; `commit_import` recomputes it from the stored source and
refuses any drift (`dry_run_stale`).

Mapping suggestions (`suggest_mapping`) are advisory only: a fixed pt-BR and
English header vocabulary, never patient values, and nothing applies until an
operator saves a mapping version. No model is called; an AI-backed (A1)
suggestion would need its own AI purpose, prompt, evaluation and an
EG-1 `approved_to_test` record first.

## Commit, provenance and reconciliation

`commit_import` records immutable batches of at most 200 rows (patients
first, then history) and queues `apps.interop.tasks.commit_batch` on the
`bulk` queue after commit. The task carries only the batch id; it resolves the
stored scope (`clinic_app.interop_batch_scope`), spends one token of the
organization's `bulk` fairness bucket (deferring with a countdown otherwise)
and commits under the requester's re-checked current authority. Each batch
re-verifies the source checksum and its own row digest before writing.

Patients are registered through intake's own invariants (name index, first
demographics version with its receipt, identifier blind indexes, duplicate
hints) under an enrollment command key the guard recomputes from the job and
row. Every imported row gets an immutable `ImportedRecord` with encrypted
source id, original author label and original time, a content digest and the
label `imported`; history (notes, observations, documents) lives only there
and is never attributed to a current user: the importer is recorded as the
actor of the transition and of intake's registration receipt, never as the
author of imported clinical content. Attachments are stored encrypted with
their SHA-256 and the synthetic quarantine scan verdict (allowlisted type,
matching signature, no active content); only clean ones can be linked.

The last batch appends `committed` with the reconciliation report: per
dataset rows, accepted, rejected and committed counts; per batch digests;
source and attachment bytes; source and attachment checksums re-verified.
Linked attachments are re-verified inside the database
(`clinic_app.interop_attachment_checks`), which returns only counts, bytes and
mismatches, never the bytes.

## Rollback (no hard delete)

`rollback_import` appends the terminal `rolled_back` transition (within the
signed rollback window after commit, step-up after approval) and calls
`clinic_app.interop_hide_reversible`, which hides every imported enrollment
that nothing else references yet. Activity is any row in the relations
listed in `migrations/_import_sql.py` (`PATIENT_ACTIVITY`,
`ENROLLMENT_ACTIVITY`) or anything beyond what the import wrote (another
enrollment, a demographics version above 1, more identifiers); in-use
enrollments are retained and returned for review. A catalog test requires
every foreign key to a patient or enrollment identity to be classified, so a
new relation cannot silently escape the check. Hidden enrollments (and their
duplicate hints) disappear from every `clinic_app`/`clinic_owner` read through
the restrictive `interop_import_visibility` policies; every row stays
physically retained. Imported history of a rolled-back job is invisible.
The clinic gate orders rollback against bookings and batches, so a rollback
may land while a commit is still in flight: batches still pending stay
`pending`, every later delivery of them returns `skipped`, and the rows
earlier batches wrote are hidden like any other unused import.

## Database and recovery obligations

All seven tables are classified in `ops/testing/restore_contract.py`. The
runtime role holds SELECT/INSERT on mappings, jobs, transitions and batches,
UPDATE only on batch `state`, `committed_count` and `committed_at`, and SELECT
on hide markers, which only the rollback resolver inserts. On stored files and
imported records it holds INSERT and SELECT only on the metadata and checksum
columns (`METADATA_COLUMNS` in `migrations/_import_sql.py`): imported content,
its source id, author and time labels, and stored source and attachment bytes
are never readable through a table, whatever the session's actor. Clinical
readers reach imported history and documents only through
`clinic_app.interop_history` and `clinic_app.interop_attachment`, which apply
`interop_record_visible` (patient-scoped `clinical.read`, live job). The
pipeline reaches the stored source only through `clinic_app.interop_source`:
while the job is in flight (`uploaded` to `approved`), for the current import
authority, with `demographics.read` once approved; it refuses everyone once the
job is committed or rolled back. The importer's own pipeline therefore still
parses the source it uploaded while the job is in flight; the database cannot
tell that step apart from other code in the same session, so it confines the
read to that resolver and those states. No machine grants. The
guard refuses DELETE, TRUNCATE and every UPDATE except the batch step.
Migration `interop.0001_import_framework` is protected and irreversible;
rollback = restore. It depends only on what it touches when it runs (identity
user, clinic and `has_permission`, intake's latest node, comms participants):
the rollback activity check is plpgsql and resolves the other relations at run
time, and a dependency on a later node of another app would make that app's
reversible migrations irreversible.

Load-bearing private imports: `intake.demographics._apply_demographics`,
`_DemographicsMutation`, `_append_identifier`, `_required_fields`,
`intake.duplicate_detection._current_identifiers`, `_current_indexes` and
`tenancy.envelope._kek`.

## Not yet delivered

- A staff UI and a migration-rehearsal onboarding step (todo 15's registry);
  both are follow-ups, so no browser suite changes here.
- Real attachment scanning, real AI suggestions and competitor layouts stay
  behind EG-1/EG-5 and EG-15.
