# AUDIT LEDGER

## OVERVIEW
Trusted-context append and independently checked hash chains; score 11 for a distinct integrity boundary and central append API.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Tenant append and public facade | `services.py` | `record_event`, `record_phase1_event`, `verify_chain` |
| Exact Phase 1A event vocabulary | `events.py` | Immutable definitions and metadata-only payload construction |
| Input normalization/hash bytes | `canonical.py` | Bounded fields, payload allowlist, trusted context |
| Recompute stored chains | `verification.py` | Content/link checks plus authorized row retrieval |
| Ledger shape | `models.py` | `AuditEvent` and reserved system organization |
| SQL append implementation | `migrations/0002_audit_append.py` | Database-side chain append |
| Immutability and verification SQL | `migrations/0003_immutability_and_verification.py` | Persistent integrity enforcement |
| Version-two boundaries | `migrations/0005_clinic_metadata_v2.py`, `_v2_*_sql.py` | Clinic metadata SQL contracts |
| Regression coverage | `../../tests/test_audit_*.py` | Append, canonicalization, migration and tamper boundaries |

## CONVENTIONS
- Tenant `record_event` derives organization and actor from connection GUCs, not caller payload.
- Both tenant and actor UUIDs are mandatory for tenant appends.
- `_record_system_event` is an owner-only path and uses `SYSTEM_ORG_ID`.
- `record_phase1_event` selects the chain from the fixed event definition.
- Phase 1A payloads contain only `clinic_id` and `object_verb`.
- Tenant verification requires an explicit organization matching the active tenant.
- `verify_chain()` without an organization means the owner-only system chain.
- Verification reads tenant rows through `clinic_app.audit_event_tenant`.
- Error messages identify fields/settings, not rejected input contents.

## ANTI-PATTERNS
- Do not substitute direct ORM insertion for the SQL append functions.
- Do not add patient names, search text, or clinical content to fixed Phase 1A events.
- Do not accept caller-supplied actor/organization as trusted append context.
- Do not interpret omitted verification organization as the current tenant.
- Do not update existing ledger rows to repair a verification failure.
- Do not change Python canonicalization without checking matching database boundary/hash behavior.
