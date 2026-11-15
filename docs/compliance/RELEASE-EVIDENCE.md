# Release-readiness evidence contract

**Status: UNAPPROVED — DO NOT USE LIVE DATA**

This document defines the machine-checked evidence contract enforced by
`ops/release/readiness.py` (`python -m ops.release.readiness check --mode
synthetic|live`). It records what accountable evidence must exist before live
data is considered; it is not legal review, not an approval, and not
permission to deploy. Passing validation never checks a box in
[LIVE-DATA-GATE.md](LIVE-DATA-GATE.md) — only the named accountable owners can
do that.

## Evidence root layout

`CLINIC_RELEASE_EVIDENCE_ROOT` names the evidence root. `CLINIC_RELEASE_ID`
labels the produced report and is required for live checks. The root holds:

```
<root>/records/<capability>.record.json   one record per capability
<root>/evidence/...                       bytes each record pins by digest
```

Each record is a closed JSON object with exactly these fields:

| Field | Requirement |
| --- | --- |
| `capability` | one of the required capabilities below |
| `owner` | named accountable owner (non-empty) |
| `approval_reference` | the approval/decision reference (non-empty) |
| `scope` | the approved scope statement (non-empty) |
| `system` | `clinic-os` |
| `environment` | the environment under evaluation (`synthetic` or `live`) |
| `issued_at` | timezone-aware ISO-8601 timestamp, not in the future |
| `review_by` | timezone-aware ISO-8601 review/expiry timestamp, still future |
| `evidence_path` | path relative to the evidence root; `..` and absolute paths are rejected, symlinks escaping the root are rejected |
| `evidence_sha256` | lowercase SHA-256 hex digest of the evidence file's bytes |
| `synthetic` | boolean; `true` marks fixture evidence and is **never accepted for live** |

Duplicate records for one capability, unrecognized capabilities, missing or
extra fields, expired review dates, digest mismatches and missing evidence
files each make the capability unsatisfied and the report not ready. For live
checks the validator also rejects evidence bytes that match any file in the
shipped synthetic fixture bundle, or that carry the fixture's self-marking
attestation, regardless of the record's `synthetic` flag — a correct digest
proves byte integrity, not that the bytes are non-fixture evidence.

## Required capabilities

The validator requires one satisfied record per capability. The set reconciles
the live-data gate items, the compliance supporting records, the task-44 plan
additions and the integration register obligations:

- Gate items: `hosted_ci`, `hosted_pitr`, `monitoring_delivery`,
  `legal_approval`, `privacy_approval`, `incident_response`,
  `credential_rotation`, `retention_policy`.
- Supporting records: `dpa`, `ropa`.
- Task-44 additions: `dpo_designation`, `dpo_public_contact`,
  `incident_record_retention`.
- Encryption/transport evidence from tasks 6/43 — backup encryption is not a
  substitute for any of these: `data_at_rest`, `tenant_key_management`,
  `managed_secrets`, `tls_transport`.
- Clinical/signature/video and provider obligations from the integration
  register: `physician_registration`, `qualified_signing`,
  `signature_verification`, `video`, `email`, `sms`, `whatsapp`, `pix`,
  `attachment_storage`, `attachment_scanning`, `pdf_rendering`.

## Attested capabilities

Three capabilities additionally require their evidence file to be a JSON
attestation carrying validated fields:

- `dpo_designation`: `formal_designation: true` and a non-empty
  `named_accountable_person`.
- `dpo_public_contact`: `published: true`, `validated: true` and a non-empty
  `public_contact`.
- `incident_record_retention`: `minimum_years` integer of at least 5,
  `measured_from: "registration"` and `covers_unnotified: true`, so every
  recorded incident — notified or not — is retained at least five years from
  registration, or longer where required.

## Modes

- `check --mode synthetic` evaluates the root (default: the checked-in
  `ops/release/synthetic-evidence/` fixture bundle) and additionally embeds a
  `live_gap_report` showing every live prerequisite the same records fail —
  synthetic evidence is never accepted for live.
- `check --mode live` requires `CLINIC_RELEASE_EVIDENCE_ROOT` and
  `CLINIC_RELEASE_ID`, evaluates records bound to `environment: "live"` and
  exits non-zero while any capability is unsatisfied.

A ready report means the supplied evidence is complete and internally
consistent. It does not establish that any approval is genuine, legally
sufficient, or applicable; legal applicability and exemptions require
documented review and never silently waive this gate.

## Capability bundle profiles

`check --mode synthetic --profile synthetic-demo` exercises the shipped
fixtures and labels every advertised feature `synthetic`. It grants no live
capability. `check --mode live --profile <name>` additionally checks the
immutable bundles in `ops/release/profiles.py`:

| Profile | Bundle beyond identity, patients, scheduling, EHR and ordinary billing | External records |
| --- | --- | --- |
| `core-live` | Private storage, scanning, PDF and the encryption/transport infrastructure | EG-1, EG-2, EG-12 |
| `intelligence-live` | AI-01, AI-02, AI-03; ASR, inference and media storage | Core + EG-3, EG-5, EG-11; passed human-reviewed evaluations |
| `revenue-live` | PIX/card PSP, NFS-e, TISS | Core + EG-5, EG-8, EG-9 |
| `regulated-rx-live` | Physician registration, qualified signing, independent verification, SNCR | Core + EG-6 |
| `network-live` | SSO/SCIM and RNDS | Core + EG-7 |

Profiles are independent extensions of core. Selecting `network-live`, for
example, does not authorize AI or payments. The provider runtime gate also
requires membership in the active profile. Diagnostics visibly label a
disabled capability **Unavailable** (`Indisponível` in pt-BR), even when a
synthetic provider has an activated-shaped test record.

The old command without `--profile` retains its complete legacy schema
check for compatibility. Its success is insufficient for live activation.
Every non-rehearsal activation and every live startup/request/job/storage
recheck uses `CLINIC_RELEASE_PROFILE` (default `core-live`). Activation
version 2 binds that profile alongside the existing evidence manifest and
storage bindings. A change or omission cannot downgrade an active profile.
Legacy activation records must be disabled and reissued; they cannot
authorize live startup under version 2. A rehearsal may exercise the legacy
validator, and remains unconditionally ineligible for live startup.

## Authenticated profile records

For each entry of `required_records`, supply exactly one JSON file at
`profiles/<profile>/<reference>.record.json` under the evidence root.
`missing_records` lists every absent or unsatisfied record in the bundle's
stable order. Unknown files ending in `.record.json` refuse the profile.
Profile records have exactly these keys:

```
reference, profile, release_id, commit_sha, tree_sha, owner,
issued_at, review_by, evidence_path, evidence_sha256, owner_signature
```

The reference, profile, release ID and full 40-character commit/tree IDs must
match the selected candidate. Set `CLINIC_RELEASE_COMMIT_SHA` and
`CLINIC_RELEASE_TREE_SHA` from the deployed build's authenticated source
receipt. Issue/review dates obey the same timezone and expiry rules as the
legacy records. Evidence is JSON, root-relative, digest-bound and cannot
escape through a symlink. Synthetic evidence cannot satisfy a live profile.

`owner_signature` has the exact format `ed25519:<128 lowercase hex
characters>`. The owner signs the RFC 8785 canonical bytes of the entire
record **without** `owner_signature`, using their separately held Ed25519
private key. `CLINIC_RELEASE_OWNER_PUBLIC_KEY` is the corresponding
32-byte public key as 64 lowercase hex characters. Install that trust anchor
through owner-controlled deployment configuration, outside the supplied
evidence. A key supplied by an approval file has no authority. The validator
never generates a signing key, reads a private key, or treats a signature
format alone as authentication. Key custody and whether an owner decision
is legally sufficient remain external responsibilities.

The signed, digest-bound JSON evidence must carry:

- `EG-n`: the exact `gate` and `decision: "approved"`.
- `release-candidate`: matching `commit_sha`/`tree_sha`, the candidate's
  numeric-string `hosted_run_id`, and `conclusion: "success"`. A prior
  baseline's run is not the candidate's evidence.
- `security-assessment`: matching commit/tree and `verdict: "accepted"`.
  Todo 73's assessment scope is preparation, not this accepted assessment.
- `disaster-recovery`: `verdict: "passed"`,
  `provider_pitr_verified: true`, and
  `encrypted_provider_backup_verified: true`. Todo 72's synthetic logical
  recovery report explicitly does not satisfy this provider evidence.
- `provider-<key>`: `version_id` and `activation_id` matching the actual
  registry's current platform version in `activated` state, bound to its
  immutable activation/approval and `live` environment. A signed claim of
  activation cannot replace the database record. The runtime role has
  SELECT only; profile activation independently checks a connection whose
  `current_user` is `clinic_owner`.
- `evaluation-AI-0n`: the matching `capability`,
  `evaluation_status: "passed"`, `human_review_complete: true`, and
  `real_world_performance: true`, an immutable UUID `run_id`, and the
  `corpus_sha256`/`thresholds_sha256` from that run. Todo 47's synthetic engineering report
  cannot stand in for the externally approved clinical evaluation.
- `reverification-RV-nn`: matching `step`, `outcome: "verified"` and a
  timezone-aware `checked_at` within the last 90 days, matching the latest
  date of a `Closed:` row in `docs/compliance/reverification-log.md`.
  An open, partly completed, stale or future retrieval refuses the profile.

Core depends on RV-04/05/06/07; intelligence adds RV-01; revenue adds
RV-11/12/13; regulated Rx adds RV-08; network adds RV-09. Re-check the
primary sources and update the log before attempting the activation that
depends on them. No source retrieval or agent-run test clears an EG gate.

Use [the release record template](../releases/premium-profile-template.md)
to create `docs/releases/<date>-premium-<profile>.md`. It binds the build,
its own hosted run and the evidence root. Local L1 evidence paths for todos
71, 75 and 76 can be cited before their changes are merged, but do not
substitute for candidate L2 or external owner records.
