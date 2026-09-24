# APPLICATION DOMAIN MAP

## OVERVIEW
Domain packages and their shared runtime helpers; score 16 for the 14-app boundary and concentrated public APIs.

## STRUCTURE
```text
apps/
|-- audit/          # Append API, canonical bytes, chain verification
|-- identity/       # Actor/role authority, authentication, owner lifecycle
|-- intake/         # Patient identity plus clinic enrollment
|-- scheduling/     # Availability, booking, agenda, transitions
|-- tenancy/        # Request/command transaction and RLS boundaries
|-- core/           # Shared fingerprints, response privacy, readiness
|-- billing/        # Invoice adapter/service placeholder
|-- comms/          # Messaging adapter/service placeholder
|-- consent/        # Consent adapter/service placeholder
|-- ehr/            # Clinical-note adapter/service placeholder
|-- interop/        # Record-exchange adapter/service placeholder
|-- prescription/   # Prescription adapter/service placeholder
|-- retention/      # Retention adapter/service placeholder
`-- teleconsult/    # Teleconsultation adapter/service placeholder
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Create-payload fingerprint | `core/idempotency.py` | Exact field sets for patient, availability, appointment |
| Patient name normalization | `core/idempotency.py` | NFC, whitespace collapse, reject control/surrogate characters |
| Response cache policy | `core/middleware.py` | Product responses private/no-store; vary Cookie and HX-Request |
| Readiness schema contract | `core/readiness.py` | Runtime role/schema, required relations, migration leaves |
| Landing/liveness/readiness | `core/views.py` | Small shared HTTP entry points |
| Future domain integrations | Stub domain `adapters.py` | Protocol boundary; no implemented service workflow |

## CONVENTIONS
- Shared create fingerprints hash the `clinic-idempotency-v1` domain plus canonical JSON.
- Canonical payloads require exact keys and canonical UUID/date/UTC-minute strings.
- `core/` has no separate app config or models; it supplies helpers to domain packages.
- Domain tests live in repository `tests/`, not nested app test packages.

## ANTI-PATTERNS
- Do not treat adapter stub service functions as working endpoints: they raise `NotImplementedError`.
- Do not replace readiness with a bare database ping; schema completeness and runtime role are checked.
- Do not exempt product error/refusal responses from the outer privacy middleware.
- Do not broaden fingerprint payloads casually; existing idempotency records encode the exact versioned contract.
