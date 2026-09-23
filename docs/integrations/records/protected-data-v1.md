# Protected data and encryption contract v1

Version: 2026-09-12-v1. Status: **implemented for the synthetic rehearsal
boundary**. The original AES-256 at-rest and tenant-scoped pgcrypto/KMS
envelope commitment is preserved and now implemented by task 43 for the
inventory classes listed below; task 44 validates separate provider evidence.

## Implemented boundary (task 43)

Field-level envelope encryption is live: `apps/tenancy/fields.py` provides
`EncryptedTextField`, `EncryptedPatientNameField`, `EncryptedDateField`,
`EncryptedJSONField` and `EncryptedBytesField`, all storing versioned tenant
envelopes in `bytea` columns through `clinic_app.protected_encrypt` /
`protected_decrypt` (tenancy migration `0004_protected_fields`). The tenant
is resolved in-database from `app.current_tenant` (staff/owner context) or
the live `app.current_patient_session` row (patient-session context, which
never carries a staff tenant GUC by design). Attachment bytes are encrypted
at the service boundary before object storage
(`apps/ehr/attachments.py`, purpose `ehr.clinicalattachment.bytes`).

Encrypted columns: `intake_patient.full_name`/`birth_date`,
`intake_patientcontact.destination`, `intake_questionnaireresponse.answers`/
`reopen_reason`, `intake_questionnaireevent.answers`/`reason`,
`ehr_clinicaldocumentversion.content` (single SOAP envelope) plus
`content_sha256` (plaintext digest used by the binding trigger for amendment
equality) and `amendment_reason`, `ehr_discarded_content.content`,
`ehr_historyassessment.reason`, `ehr_problem.description`,
`ehr_allergy.description`, `consent_consenttext.text`, all eight
`prescription_prescriptionitem` text columns,
`prescription_prescriptiondocument.pdf_bytes`/`frozen_input`,
`prescription_signatureoperation.signed_bytes`, and
`billing_pixcharge.copy_code`/`qr_base64`.

Prescription document payloads are envelopes too: the anonymous
`prescription_verify` resolver and the patient document resolvers take the
KEK as a call parameter and decrypt inside the resolver boundary through
`clinic_app.prescription_document_open`, which binds `app.current_tenant`
to the row's own organization for the decrypt call only. `clinic_resolver`
holds EXECUTE on `tenant_decrypt` for exactly this path; the runtime role's
grants are unchanged. Stored digests still cover plaintext, so independent
verification and authorized download return the exact original bytes.

Existing attachment objects migrate through the owner-side
`encrypt_attachment_objects` command
(`apps/ehr/attachment_migration.py`): each stored object is classified
(envelope, legacy plaintext matching its recorded digest, or unreadable),
legacy bytes are re-encrypted and atomically replaced under the same
storage key, and the run emits a payload-free JSON receipt. The command is
idempotent and resumable.

Patient registry search runs inside the database boundary:
`clinic_app.patient_registry_count`/`patient_registry_page` decrypt,
normalize-compare, order and paginate in-database, re-checking the caller's
manager role; only the authorized page's plaintext crosses to the
application. This is the approved search approach: no encrypted index, no
plaintext shadow column, no unrestricted decrypt-all to the app.

Deliberately plaintext (documented decisions):
`teleconsult_teleconsultroom.room_name`
is the deterministic operational identifier `tc-<session uuid>`; audit
ledger rows stay canonical plaintext per the hash-chain contract; staff
identity/OTP fields remain blocked on the unresolved identity-key-scope
decision recorded below.

Migrations are one-way: rename-to-`*_legacy`, add `bytea`, then a backfill
encrypts each row in-database with a decrypt-verify per row, seals with a
completeness check, and drops legacy columns. Each table's backfill runs
inside one transaction: RLS and user triggers are suspended only inside
that transaction (catalog changes are transactional, so concurrent
sessions never observe an unprotected table and interruption rolls the
toggles back with the data), which also keeps immutable/transition guards
and receipt triggers from firing on a mechanical re-encryption.
Rollback restores from the owned recovery target; it never reactivates
plaintext operation. Production requires `CLINIC_SECRET_BACKEND` at startup
(`config/settings/prod.py`); there is no plaintext fallback.

## Current source boundary

## Current source boundary

Current patient data is synthetic. [Patient models](../../../apps/intake/models.py)
store full_name and birth_date; [patient search](../../../apps/intake/patient_search.py)
uses those values directly. Scheduling uses relational IDs and timestamp range
constraints. [Security](../../SECURITY.md) describes the trusted organization
context, clinic roles, audit allowlist and unapplied encryption infrastructure.
The communications, EHR, consent, prescription and billing domains still contain
planned/stub surfaces. Their rows below are required future coverage, not claims
that those model fields already exist.

"Tenant" means organization, matching the existing app.current_tenant/RLS
boundary. Clinic access remains a separate authorization check within that
organization. A clinic ID supplied by a caller cannot select or unwrap a key.

## Field and object inventory

All persisted classes require approved AES-256 storage coverage. "Envelope"
below additionally requires the reviewed tenant/principal payload contract;
infrastructure encryption alone does not satisfy that requirement.

| Class / source | Present or planned | Protection and compatibility requirement |
| --- | --- | --- |
| Patient identity: full_name, birth_date; intake Patient | Present | Organization envelope; preserve exact normalization, search, display and enrollment semantics before migration |
| Patient contact destinations, verification evidence, preference history | Planned tasks 14/15 | Organization envelope for destinations/evidence; minimal status/version metadata may remain queryable only under approved RLS policy; changed destination invalidates verification |
| Staff user identity and django-otp TOTP secrets | Present identity/OTP framework | Sensitive fields/seeds require approved identity-scoped protection; a staff user can belong to several organizations, so choosing one clinic's key is invalid; identity key scope and lookup/OTP compatibility are unresolved owner decisions |
| Password hashes, invitation token hashes and session authentication state | Password/session present; patient invitations planned 15 | Retain reviewed one-way password/token hashing and session integrity; never replace hashes with decryptable secrets. Protect recoverable secrets at rest with approved principal scope; do not persist raw invitation secrets |
| Patient IDs/enrollments, organization/clinic/practitioner relations, appointment times/status/cancellation codes, availability | Present; waitlist planned 18 | AES-256 storage plus RLS; relational/query columns must retain FK, uniqueness, ordering and overlap constraints. Field-envelope exclusions for minimal operational metadata require explicit security approval; free-text or identifying additions require envelope coverage |
| Idempotency fingerprints/operation IDs and provider mappings | Foundation present; jobs/callbacks planned 13 | Preserve replay/conflict semantics; assess low-entropy fingerprint inference and any new indexes. Encrypt sensitive destinations/provider payloads; keep only reviewed minimal routing references queryable |
| Questionnaire templates, responses, saved drafts and transition receipts | Present synthetic task 16; envelope implementation remains task 43 | Organization envelope for typed answers/drafts and receipt answer snapshots/reopen reasons; exact immutable template version and clinician-only response/history access retained. Template labels/options are clinic configuration, never medical advice. |
| Encounter notes, SOAP/specialty content, problems/allergies, provenance and amendments | Planned 20-24 | Organization envelope for clinical payloads; immutable signed/superseded content and amendment lineage remain byte/version stable |
| Uploaded attachment bytes, filenames and sensitive media metadata | Planned 23 | Object envelope plus encrypted quarantine/accepted storage; authorize before decryption; scan exact bytes and promote only matching digest; no pre-scan retrieval |
| Consent text snapshots, subject evidence, acceptance/revocation receipts, IP/device evidence | Planned 26/41 | Envelope for subject-linked evidence; immutable accepted text/version/digest retained; revocation creates history rather than erasing receipts |
| Teleconsultation credentials, room/participant references and encounter notes | Planned 27-30 | Short-lived tokens remain out of logs/URLs where avoidable; persist only required encrypted references/evidence. Automatic recording/transcription excluded, so no recording-store capability is implied |
| Prescriptions, reviewed PDFs, signed bytes, certificate/verification reports and private QR resolution metadata | Planned 31-36 | Organization object/payload envelope; decrypt to exact original bytes for independent verification and authorized download. Storage encryption must not rerender, resign or alter document digest/validity |
| PIX debtor identifiers, provider references, invoice/receipt payloads | Planned 37-40 | Envelope for personal/provider payloads; reviewed amount/currency/status/reference columns remain usable for exact reconciliation under tenant controls; never store payment secrets in audit |
| Clinic templates/branding and consent overlays | Planned 41 | Classify public assets separately; private/versioned clinical/consent configuration and any identifying content require appropriate envelope/access controls |
| Controlled record exports and temporary render/scan/export files | Planned 25/33/35/43 | Encrypted private staging and object storage, bounded lifetime/access and task-owned cleanup. Key absence is a failure; no unencrypted spill-to-disk fallback |
| Audit ledger and canonical hash-chain inputs | Present; vocabulary grows per domain | Retain metadata-only allowlist and immutable hashes/bytes; encrypted database/backups plus RLS. Do not encrypt/rewrite historical canonical rows in place and invalidate the chain; any changed representation requires a separately reviewed versioned design |
| Database/WAL/replicas, volumes, backups/snapshots and object versions | Current local synthetic; hosted planned 43 | AES-256 across primary/replica/backup/restore paths, including historical keys and held versions; backup encryption is separately evidenced from active storage |
| Logs, traces, queues/caches, crash dumps and swap | Current tooling; hosted policy planned 43/44 | Exclude secrets/clinical payloads by design; residual authorized metadata storage is encrypted and retained under reviewed policy. Disable sensitive core dumps; bound cache/queue persistence and prove no accidental payload spill |

Every new sensitive field/object added after this inventory must be classified
before task 43's migration is approved. Keys, plaintext payloads and provider
credentials are never copied into source control, documentation or QA evidence.

## Search and relational compatibility

The existing patient service normalizes names, requires a 2-100 character query,
applies case-insensitive substring search inside the authorized selected clinic,
optionally filters exact birth_date, orders by Lower(full_name), birth_date and
patient UUID, and returns 25-row pages with an exact total/page count. These are
observable compatibility requirements, not optional conveniences. Registration
does not make name/birth date a unique identity or merge equal people.

Randomized ciphertext cannot directly support the existing substring/order
queries. No encrypted index or custom searchable-encryption scheme is selected
here. Before task 43, the security/data owner must approve a maintained approach
and document its leakage, query semantics, tenant/clinic confinement, bounded
resource use, index migration and query-plan evidence. Deterministic encryption,
plaintext shadow columns, unrestricted decrypt-all searches and new lookup
hashes are not implicitly authorized by this document.

The approved implementation must prove identical authorized results, ordering,
pagination and normalization on a representative synthetic dataset, plus denied
cross-tenant/key access. If it cannot preserve those semantics, task 43 remains
blocked on that decision rather than silently changing search or retaining a
plaintext fallback. Keep scheduling range exclusions, FK/RLS boundaries and
idempotency behavior independently tested.

## Key, primitive and authorization contract

The selected design must use reviewed maintained primitives and libraries.
Record exact AES-256 profile, integrity protection, encoding and dependency
versions for pgcrypto fields and object envelopes. pgcrypto offers AES-256 but
does not select it by default, and its raw encryption functions lack integrity
checks. Server-side pgcrypto retains the database/system administrator trust
boundary and needs verified transport. No custom cipher, IV/nonce generator,
signature verifier or raw encryption composition is authorized.
[PostgreSQL 16 reference](https://www.postgresql.org/docs/16/pgcrypto.html).

Require tenant-bound DEKs wrapped by an approved KMS KEK; persist only wrapped
DEK, ciphertext, algorithm/envelope version, key identifier/version and reviewed
context metadata. The concrete authenticated context and integrity mechanism
must bind organization, object/field identity and purpose to the approved
library/provider profile. Do not assume a plain pgcrypto encrypted value
automatically supplies KMS tenant context or an authenticated object envelope.

KMS permissions and unwrap operations must derive from trusted actor/service
authority. Separate migration/backup/key administration from ordinary runtime
read/write access and log payload-free key access decisions. A valid key does
not bypass RLS, clinic role, patient ownership, consent or document-release
checks. Missing, wrong-tenant, disabled, retired-without-history or inaccessible
keys must fail closed. In-memory secret lifetime/cache and zeroization limits
must be documented for the chosen runtime; do not promise perfect erasure.

## Migration, rollback and rotation

Task 43 requires a reviewed schema/RLS and data-migration plan before executing:

1. Inventory every source field/object/version, approved query exception and
   destination envelope format. Establish an owned encrypted recovery target
   and recoverable historical keys before changes.
2. Introduce versioned encrypted fields/objects and constrained key references
   through reviewed additive migrations. Bind each batch to organization and
   an immutable source version/digest; make progress resumable and idempotent.
3. Encrypt existing authorized synthetic payloads in bounded batches, verify
   decrypted equality and object/document digests, and record counts/failures
   without payloads. Recheck source version to reject concurrent change.
4. Stage new writes through the approved encryption path and verify complete
   backfill/search/RLS before switching reads. Any temporary legacy
   representation must be explicitly scoped to migration, encrypted at the
   storage layer and inaccessible as an ordinary runtime fallback.
5. Prove rollback using retained encrypted versions and the owned restored
   target. Rollback disables affected writes or returns to a verified encrypted
   version; it must not reactivate plaintext operation or destroy originals.
6. Remove obsolete plaintext columns/copies only after accountable approval,
   verified recovery and legal-hold/retention review. Handle old WAL/backups,
   exported copies and object versions; schema deletion is not secure erasure.

Distinguish KEK rotation/rewrapping from DEK rotation/reencryption. Preserve
logical document bytes, hashes, signatures, audit history and held versions.
Record key version, partial progress, concurrent-write policy and failures.
KMS automatic rotation does not rotate application data keys or reencrypt old
payloads by itself.
[KMS rotation reference](https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html).

Restoration must recover data and every required historical wrapping/data-key
version into an authorized isolated target, rebuild only approved indexes and
verify authorized decryption, patient search, document signatures, audit chains,
counts, RLS and clinic_app role. Missing key history is recovery failure.
Key deletion cannot bypass legal hold or approved retention.

## Evidence required by tasks 43 and 44

Task 43 captures actual selected-provider configuration and encrypted
round trips, corruption/wrong-context/wrong-key denial, search compatibility,
migration interruption/resume, rollback, secret retrieval/rotation and key/data
restoration. Provider PITR gets a separate authorized target, measured recovery
time/loss and cleanup receipt. No production resource is overwritten.

Task 44 binds independent data_at_rest, tenant_key_management, managed_secrets,
tls_transport and hosted_pitr records to accountable owners, system/environment,
approval references, dates/review, exact evidence bytes/digests and synthetic
status. A local logical restore, environment variable, unsigned PDF, storage
encryption checkbox or test fixture cannot serve as real deployment approval.
