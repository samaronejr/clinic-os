# Independent security assessment: scope package

- Status: scope only, prepared 2026-11-11 (todo 73). No external assessment
  has been commissioned or performed. Nothing here satisfies EG-12
  ("Independent security assessment, incident and recovery approvals, provider
  PITR") or clears an item in
  [LIVE-DATA-GATE.md](../compliance/LIVE-DATA-GATE.md). Only the owner can
  commission the assessment, sign the authorization below and accept its
  report.
- Data: synthetic only. The assessed environment holds no real patient,
  staff or provider data, and every external integration runs on its
  synthetic or recorded-fixture adapter.

## Target

| Item | Value |
| --- | --- |
| Build | The release-candidate commit the owner names in the authorization; the assessor records its full SHA |
| Dependencies | [CycloneDX runtime SBOM](../releases/sbom/clinic-os-runtime.cdx.json) from that commit (`python -m ops.release.sbom check` must pass) |
| Environment | One task-owned deployment built from that commit: the candidate image, PostgreSQL 16 with the bootstrap roles, Redis, the WSGI web process, the ASGI realtime process and the Celery worker, reachable only by the assessor |
| Configuration | `CLINIC_DATA_MODE=synthetic`; every `*_SYNTHETIC_*` gate an exercised surface needs is on; no provider capability is `activated` |
| Not in scope | Production or any shared environment, Terraform (validate-only skeleton, never planned or applied), real provider endpoints, hosting-provider controls, physical and social-engineering tests |

## Surfaces

| Surface | Entry points | Main controls to challenge | Threat model |
| --- | --- | --- | --- |
| Staff web workspace | Server-rendered and HTMX pages under `/workspace/` and every app route mounted in `config/urls.py` | Session auth, TOTP and passkey step-up, CSRF, CSP, tenant transaction per request, identical unknown/foreign denials, record selectors in POST bodies | All |
| Authentication | `/auth/` login, verify, step-up, passkeys, `/auth/sso/begin/` and `/auth/sso/finish/` | Credential and OTP throttling and replay, single-use OIDC state with PKCE, session rotation, no claim-based elevation | [support-access](../threat-models/support-access.md) |
| Internal UI API | POST-only JSON under `/api/ui/v1/` ([contract](../api/ui-v1.yaml)) | `{code, message_key}` refusals, `access_denied` parity, bounded inputs, no exception detail in 500s | All |
| Partner API | `/api/v1/` and `oauth/token` ([contract](../api/external-v1.yaml)), webhook subscriptions | Digest-only client secrets, scopes, opaque external ids, rate limits, idempotency keys, signed webhooks | [import-export](../threat-models/import-export.md) |
| Provider callbacks | `/comms/webhooks/...`, `/orders/lab/callback/`, `/prescription/signing/callback/...`, `/auth/identity-notification/callback/`, workflow and PSP callbacks | Authentication on raw bytes before scope lookup, payload tenant claims ignored, replay convergence | [payments](../threat-models/payments.md) |
| Realtime | `POST /rt/stream` ticket, ASGI stream | Single-use tickets, topic authorization in a short tenant transaction, event schema `{topic_hash, kind, version}`, revocation closes streams | [realtime](../threat-models/realtime.md) |
| Patient portal and delegates | Patient-session routes, public booking, `prescription/verify/<handle>/` | Patient-session authority, delegate scope and expiry, restricted records, public verification exposing no PHI | [delegates](../threat-models/delegates.md) |
| Uploads and media | EHR attachments, scribe chunk ingest, interop imports | Size and type bounds, quarantine scanning, chunk identity and epochs, archive layout allowlist, encryption at rest | [scribe-media](../threat-models/scribe-media.md), [import-export](../threat-models/import-export.md) |
| AI and agents | Brief, scribe drafting, decision support, agent proposals and approvals | Kill switches, budgets, egress allowlist, closed output schemas, digest-bound approvals, prompt-injection corpus | [ai-gateway](../threat-models/ai-gateway.md), [agents](../threat-models/agents.md) |
| Support and break-glass | Support grants and diagnostics, SCIM endpoints, emergency access | Customer approval, purpose and expiry, masking, SCIM token budget and revocation, four-hour break-glass cap | [support-access](../threat-models/support-access.md) |
| Operations endpoints | `/healthz`, `/readyz`, `/internal/metrics`, `sw.js` | Token and network allowlist on metrics, no clinical content in the service-worker cache | - |
| Database (white-box) | Read-only review of `ops/db/bootstrap.sql`, migrations and `make db-posture` output | Role separation, FORCE RLS, SECURITY DEFINER resolvers, no hard deletes | [SECURITY.md](../SECURITY.md) |

Each threat model's mitigation table lists the tests that prove its controls,
and `python -m ops.testing.threat_model_coverage report --junit <pytest
junit.xml> --output threat-coverage.json` calls a mitigation covered only when
a mapped test passed in that run. Rows marked `**Gap:**` name controls known
to be missing (DG-R1: the patient access log does not show delegate access),
and rows marked `**Unproved:**` name clauses no test proves (IE-R1, PY-R1,
PY-D1, SM-I1, SA-T1); neither counts as covered, and the assessor should treat
them as open findings. The assessor should test the
controls directly, not rely on these tests.

## Roles and test accounts

The assessor gets one synthetic account per role in two organizations, so
that cross-tenant tests have a real target. The owner provisions them with
`make bootstrap-clinic` and `make provision-staff` on the assessed deployment
only and delivers credentials and TOTP enrolment out of band. Credentials,
OTP seeds and tokens never appear in tickets, reports or this repository.

| Principal | Accounts | Notes |
| --- | --- | --- |
| Staff roles | `owner`, `org_admin`, `clinic_admin`, `clinic_manager`, `finance`, `physician`, `nurse`, `allied_professional`, `receptionist`, `scheduler` in organization A; `clinic_admin`, `physician`, `receptionist` in organization B | Privileged roles enrol TOTP; one physician also registers a passkey |
| Patient and delegate | One synthetic patient session, one verified guardian grant, one expired grant | Patient records named "Synthetic ..." only |
| Support | One support user with a customer-approved grant and one without | |
| Service principal | One `clinic_agent` principal with clinic-scoped availability grants | Database access only; no web login exists |
| Partner API | One active client per organization and one revoked client | Client secrets issued by the owner CLI |

## Rules of engagement

1. Written authorization signed by the owner names the assessor, the commit,
   the environment, the window and the contacts before testing starts.
2. Testing stays inside the named environment and window. Out-of-scope
   systems, including every real provider and the hosting account, are not
   touched; a request that would reach one stops at the synthetic adapter.
3. Load and denial-of-service tests stay under the rate the authorization
   agrees. Destructive database actions, data deletion and attempts to break
   out of the container or host need separate written approval.
4. Only synthetic data is created, read or exported. Any real-looking
   personal data found is reported at once and not copied.
5. A suspected critical finding (tenant leakage, authentication bypass,
   remote code execution, audit-chain tampering) is reported to the owner
   within one business day through the private incident channel in
   [SECURITY.md](../SECURITY.md#incident-response), and testing of that path
   pauses until the owner replies.
6. The assessor keeps evidence free of credentials, session cookies and
   tokens, and deletes all copies at the end of the engagement.

## Expected deliverables

- A report naming the commit, environment, window, method and tools, with
  each finding's surface, preconditions, reproduction steps, impact and a
  CVSS v4.0 score.
- A retest of every fixed finding on a later commit.
- An attestation of which surfaces above were tested and which were not.

The owner records the accepted report as an EG-12 approval record under
`docs/compliance/` only after reviewing it. Agents prepare and update this
scope; they never certify the assessment or tick a LIVE-DATA-GATE box.
