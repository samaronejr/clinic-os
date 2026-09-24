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
