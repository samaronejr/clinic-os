# Integration capabilities

Superseded in part by docs/plans/clinic-ops-premium-successor.md (SD ledger).

Record set: **2026-09-24-v2**, which supersedes
[2026-09-12-v1](records/2026-09-12-v1/) (retained unedited). Status:
**documentation complete; all 24 external capabilities unavailable**. No
provider, package, sandbox, processing agreement, accountable owner or live approval has been supplied. These records authorize
no purchases, outbound patient messages, provider activation or live data.

The canonical renewal plan owns task ordering. This is a versioned contract
register, not another implementation checklist. Communications now implement
[synthetic appointment reminders](../../apps/comms/README.md) with independent
channel gates; no real messaging provider is authorized. Video, prescription
and billing adapters remain protocol stubs; see
[architecture](../ARCHITECTURE.md), [security](../SECURITY.md) and the
[unapproved live-data gate](../compliance/LIVE-DATA-GATE.md).

## Current records

| Capability | Record | Dependent renewal tasks | Successor todos |
| --- | --- | --- | --- |
| `email` | [Email delivery](records/2026-09-24-v2/email.md) | 13, 14, 19, 44 | 4, 51 |
| `sms` | [SMS delivery](records/2026-09-24-v2/sms.md) | 13, 14, 19, 44 | 4, 51 |
| `whatsapp` | [WhatsApp delivery](records/2026-09-24-v2/whatsapp.md) | 13, 14, 19, 41, 44 | 4, 51, 54 |
| `video` | [Video consultation](records/2026-09-24-v2/video.md) | 13, 27, 28, 29, 30, 43, 44 | 4, 37 |
| `physician_registration` | [Physician registration verification](records/2026-09-24-v2/physician_registration.md) | 13, 31, 32, 34, 44 | 4, 15 |
| `pdf_rendering` | [PDF rendering](records/2026-09-24-v2/pdf_rendering.md) | 33, 34, 35, 36, 44 | 33, 35 |
| `qualified_signing` | [Qualified document signing](records/2026-09-24-v2/qualified_signing.md) | 31, 32, 33, 34, 35, 36, 44 | 4, 35 |
| `signature_verification` | [Independent signature verification](records/2026-09-24-v2/signature_verification.md) | 33, 34, 35, 36, 44 | 35 |
| `pix` | [PIX charging and reconciliation](records/2026-09-24-v2/pix.md) | 13, 37, 38, 39, 40, 43, 44 | 4, 56, 57 |
| `attachment_storage` | [Private attachment and document storage](records/2026-09-24-v2/attachment_storage.md) | 23, 25, 33, 35, 43, 44 | 40, 72 |
| `attachment_scanning` | [Attachment malware scanning](records/2026-09-24-v2/attachment_scanning.md) | 23, 25, 43, 44 | 4, 40 |
| `hosted_pitr` | [Hosted encrypted backup and point-in-time recovery](records/2026-09-24-v2/hosted_pitr.md) | 43, 44, 46, 47 | 72 |
| `data_at_rest` | [AES-256 storage encryption](records/2026-09-24-v2/data_at_rest.md) | 43 implementation; 44 evidence; 46/47 live use | 72, 73 |
| `tenant_key_management` | [Tenant-scoped pgcrypto/KMS envelope encryption](records/2026-09-24-v2/tenant_key_management.md) | 43 implementation; 44 evidence; 46/47 live use | 72 |
| `managed_secrets` | [Managed secret loading and rotation](records/2026-09-24-v2/managed_secrets.md) | 13 external effects; 43 implementation; 44 evidence; 46/47 live use | 73 |
| `tls_transport` | [TLS transport policy](records/2026-09-24-v2/tls_transport.md) | 27, 38 external transports; 43 implementation; 44 evidence; 46/47 live use | 73 |
| `asr` | [Speech recognition (pt-BR)](records/2026-09-24-v2/asr.md) | none (new in 2026-09-24-v2) | 4, 38, 41, 48 |
| `llm_inference` | [LLM inference for drafting and assistance](records/2026-09-24-v2/llm_inference.md) | none (new in 2026-09-24-v2) | 4, 38, 42, 44, 45, 46, 48 |
| `sncr` | [SNCR controlled-prescription integration](records/2026-09-24-v2/sncr.md) | none (new in 2026-09-24-v2) | 4, 34, 36 |
| `rnds` | [RNDS national health data exchange](records/2026-09-24-v2/rnds.md) | none (new in 2026-09-24-v2) | 4, 68 |
| `tiss` | [TISS insurance exchange](records/2026-09-24-v2/tiss.md) | none (new in 2026-09-24-v2) | 4, 59 |
| `nfse` | [NFS-e fiscal documents](records/2026-09-24-v2/nfse.md) | none (new in 2026-09-24-v2) | 4, 58 |
| `psp_card` | [Card payments, installments and split](records/2026-09-24-v2/psp_card.md) | none (new in 2026-09-24-v2) | 4, 56, 57 |
| `object_storage_media` | [Encrypted object storage for media and AI artifacts](records/2026-09-24-v2/object_storage_media.md) | none (new in 2026-09-24-v2) | 4, 40, 43, 72 |

The [protected data inventory](records/protected-data-v1.md) specifies required
coverage and the search, migration, key rotation and restoration decisions that
must precede task 43. It is a contract, not evidence of encryption in operation.

## Reading and updating a record

Each record names a candidate or example, source date/version, owner decision,
protocol gaps, formats, callback/idempotency requirements, package approval,
sandbox fixtures, residency/retention and costs. "Unavailable" means no owner
authorization or executed provider evidence exists. A documentation link is
research evidence only. All source observations were retrieved in the
2026-09-12 official-source intake; targeted encryption, Daily, Asaas and VALIDAR
sources were rechecked while writing this set. Mutable pages require a fresh
review when a provider is selected and before release.

Historical candidates are Daily, Asaas, BirdID/VIDaaS/Memed, CFM and messaging
providers. SES, Twilio, WeasyPrint, AWS infrastructure and ClamD are reference
examples, not selections. Historical prices, retention/legal interpretations,
recording requirements and controlled-prescription dates are not copied as
current commitments. The renewal plan's corrected scope takes precedence;
controlled prescriptions and automatic recording/transcription remain excluded.
Superseded in part by docs/plans/clinic-ops-premium-successor.md (SD ledger):
SD-1, SD-4, SD-6 and SD-12 turn these exclusions into gated successor scope;
every record above still stays unavailable.

Retain this dated record set. A selection or changed protocol gets a new dated
version with a supersedes link, exact provider/API/package versions and owner
approval reference. A named owner must supply scope, system/environment, date,
review/expiry, approved package/version and budget, residency/subprocessors,
retention/legal-hold behavior, processing agreement and sandbox authority.
Record unavailable fields honestly; do not invent an owner or infer approval
from an installed library. Secrets and identifying payloads never belong here.

## Integration and release boundaries

Task 13 implements common committed outbox/retry behavior. Network calls occur
outside tenant request transactions; execution rechecks authority and current
contact/consent state. Authenticate callbacks before resolving stored operation
and tenant. Provider-controlled tenant IDs are not authority. Each operation
has bounded retry and explicit ambiguous-result reconciliation; a local UUID
does not prove the remote creation endpoint is idempotent.

Provider tasks 19, 23, 27, 31, 33, 34 and 38 may implement their generic synthetic
slice while the matching external action remains unavailable. Synthetic
fixtures cannot be copied into approvals. Task 6 completion means the register
and gaps are documented, not that any integration is ready.

Task 43 owns selected encryption/storage/secrets implementation and measured
recovery. Task 44 requires separate `data_at_rest`, `tenant_key_management`,
`managed_secrets`, `tls_transport` and provider-PITR evidence. Neither backup
encryption nor a logical restore substitutes for the other capabilities.
The later release record schema is capability, owner, approval_reference, scope,
system/environment, issued_at, review_by, evidence_path, evidence_sha256 and
synthetic flag; these planning records are not those approval records.
Tasks 46/47 remain dependent on accountable live approvals and actual evidence.

## Missing-capability decisions

| Observed condition | Required outcome | Decision owner |
| --- | --- | --- |
| No authorized sandbox for any current record | No ready provider claims; generic synthetic work remains available | Unassigned capability owner listed in the record |
| PDF renderer package unapproved | Task 33 real rendering unavailable; do not install a package or substitute browser print as verified output | Unassigned technical package owner |
| Signing provider returns bytes but no independent verifier is approved | Tasks 34-36 cannot mark a document verified or issue it as valid | Unassigned clinical/signature trust-policy owner |
| Asaas-style callback uses a token rather than HMAC | Use only the approved provider's exact authentication contract; do not accept another provider's validator | Unassigned billing/integration owner |
| KMS storage encryption exists but application envelopes/search/rotation do not | Tenant key capability remains unavailable; task 44 live readiness fails | Unassigned security/migration owner |
| Secret manager, key or certificate validation unavailable | Dependent production action fails closed; no plaintext/credential fallback | Unassigned infrastructure/security owner |

This table describes required later behavior. It does not claim a runtime
capability validator or provider adapter already exists.
