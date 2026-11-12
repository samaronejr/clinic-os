# Threat model: guardian and delegate access

- Status: design baseline, 2026-09-24 (todo 2). Mitigations are planned work
  owned by the listed todos unless a current source path is named. The Tests
  column maps each mitigation id to the tests that prove it (todo 73,
  checked by `tests/infra/test_threat_model_coverage.py`). A mitigation that
  starts with **Proposal:** goes beyond the owning todo's plan text; that
  todo accepts or rejects it and isn't bound by it until then.
- Decisions: [ADR-003](../adr/ADR-003-authorization-permission-bundles.md)

## Scope

People acting for a represented patient: legal guardians of minors, curators
and patient-authorized adults. Covers `PatientDelegateGrant`, its verification
by staff, the patient session field `acting_for_enrollment_id`, portal
surfaces and delegate messaging. Patient self-access is already governed by
`apps/intake/patient_access.py` and `docs/clinical/patient-access.md`.

## Assets

- The represented patient's records, results, documents and messages.
- Records flagged `restricted_from_delegates` (psychiatry, adolescent
  confidentiality per clinic policy).
- Evidence documents proving authority (in quarantine storage).
- Grant state and validity windows.

## Trust boundaries

1. Delegate browser to the patient portal (patient session).
2. Grant request and evidence upload to staff verification.
3. Portal requests to domain services (grant re-validated per request).
4. Outbound messages to the delegate's contact channel.

## Data flow

```mermaid
flowchart LR
  subgraph Delegate["Delegate browser (untrusted)"]
    Portal["Patient portal, delegate switch"]
  end
  subgraph Staff["Staff browser"]
    Verifier["Verifier staff"]
  end
  subgraph App["Clinic Ops web"]
    Request["request_delegate_grant with evidence"]
    Verify["verify and activate grant"]
    Session["Patient session with acting_for_enrollment_id"]
    Check["Per-request grant check, restricted filter"]
  end
  subgraph Data["Data stores"]
    PG[("PostgreSQL: PatientDelegateGrant, FORCE RLS")]
    Q[("Quarantine storage: evidence documents")]
  end
  Portal --> Request
  Request --> PG
  Request --> Q
  Verifier --> Verify
  Verify --> PG
  Portal -- "switch to represented patient" --> Session
  Session --> Check
  Check --> PG
  Check -- "permitted, non-restricted content" --> Portal
```

## Threats and mitigations

| ID | STRIDE | Threat | Mitigation | Todos | Tests |
| --- | --- | --- | --- | --- | --- |
| DG-S1 | Spoofing | Someone claims guardianship because they hold the patient's phone or email. | Contact possession never grants authority; grants start `pending_verification` and need staff verification against evidence. | 19 | `tests/patients/test_delegates.py::test_rejected_evidence_never_activates`<br>`tests/patients/test_delegates.py::test_delegate_sm_verification_idempotency_and_stale_commands` |
| DG-S2 | Spoofing | A forged evidence document is accepted. | Evidence goes to quarantine storage and verifier staff record the decision; the verifier identity is kept on the grant. | 19 | `tests/patients/test_delegates.py::test_delegate_sm_verification_idempotency_and_stale_commands`<br>`tests/patients/test_delegates.py::test_delegate_database_shape_is_exact_and_runtime_cannot_write_bindings` |
| DG-T1 | Tampering | The delegate edits the grant scope or switches to an enrollment they don't hold. | Session switch re-validates the grant binding on every request; scope is a subset of `PatientAccessGrant` operations and never widens. | 19 | `tests/patients/test_delegates.py::test_delegate_scope_is_stored_and_cannot_widen_or_cross_clinic`<br>`tests/patients/test_delegates.py::test_committed_revocation_denies_a_previously_bound_open_request` |
| DG-R1 | Repudiation | A patient disputes who viewed their record through a delegate. | Grant transitions and delegate actions append registered audit events; the patient-visible access log shows each delegate read (grant, record type, time) next to break-glass reads. | 19, 50, 73 | `tests/security/test_delegate_access_log.py::test_delegate_read_appears_in_the_represented_patients_access_log`<br>`tests/security/test_delegate_access_log.py::test_delegate_result_read_appears_in_the_represented_patients_access_log`<br>`tests/security/test_delegate_authority.py::test_delegate_session_switch_appends_a_metadata_only_audit_event`<br>`tests/patients/test_delegates.py::test_delegate_sm_verification_idempotency_and_stale_commands` |
| DG-I1 | Information disclosure | A guardian reads restricted content such as adolescent confidential notes. | Records flagged `restricted_from_delegates` are never exposed to delegate sessions; the flag policy is reviewed under EG-2. | 19 | `tests/patients/test_delegates.py::test_restricted_records_are_hidden_in_database_and_unknown_denials_match`<br>`tests/patients/test_dsar.py::test_delegate_export_excludes_restricted_records_and_foreign_requests`<br>`tests/renewal/test_document_verification.py::test_delegate_cannot_list_or_export_a_restricted_signed_document` |
| DG-I2 | Information disclosure | Results are released to a delegate before the clinician releases them. | Result release is per audience and happens only after clinician acknowledgment and release. | 31, 55 | `tests/orders/test_delegate_sources.py::test_delegate_requires_its_exact_release_and_revocation_hides_source`<br>`tests/orders/test_patient_sources.py::test_patient_sources_require_current_acknowledgment_and_exact_patient_release`<br>`tests/workflows/test_urgent_queue.py::test_guardian_access_change_re_evaluates_release_scope` |
| DG-I3 | Information disclosure | Messages meant for the patient go to a delegate, or the reverse. | Messaging purpose and consent are per recipient; delegate context is explicit in the portal banner and in message routing. | 20, 50, 51 | `tests/comms/test_portal_messages.py::test_a_verified_delegate_with_message_scope_reads_and_replies_for_the_patient`<br>`tests/comms/test_portal_messages.py::test_sessions_and_delegates_without_the_messages_scope_see_no_thread`<br>`tests/patients/test_delegates.py::test_delegate_consent_keeps_exact_purpose_and_truthful_authority` |
| DG-D1 | Denial of service | A hostile party revokes or floods grant requests to block the real guardian. | Revocation authority is limited to the grantor and verifier staff. **Proposal:** rate limit grant requests per patient session. | 19 | `tests/security/test_delegate_authority.py::test_only_verifier_staff_or_the_grantor_can_revoke_a_delegate_grant`<br>`tests/patients/test_delegate_membership.py::test_open_staff_context_refuses_delegate_commands_after_membership_revocation`<br>`tests/patients/test_delegates.py::test_delegate_native_authority_refuses_inactive_staff_with_patient_context` |
| DG-E1 | Elevation of privilege | An expired or revoked grant keeps working in an open session. | Expiry is checked on every use (equality counts as expired); revocation re-validates sessions and closes realtime streams. | 8, 19 | `tests/patients/test_delegates.py::test_revoke_and_expiry_deny_next_request_and_renewal_stays_pending`<br>`tests/patients/test_delegates.py::test_committed_revocation_denies_a_previously_bound_open_request`<br>`tests/realtime/test_topics.py::test_patient_topics_bind_exact_enrollment_and_operation` |
| DG-E2 | Elevation of privilege | Blanket guardian access covers every record and operation. | Grants carry purposes and allowed operations; there is no blanket grant type. | 19 | `tests/patients/test_delegates.py::test_malformed_scope_cannot_create_authority`<br>`tests/patients/test_delegates.py::test_delegate_scope_is_stored_and_cannot_widen_or_cross_clinic`<br>`tests/patients/test_dsar.py::test_delegate_operations_and_purposes_bound_their_requests` |

## Residual risk and external gates

- Confidentiality rules for adolescents and psychiatric records depend on
  clinic policy and legal review under EG-2.
- Staff verification quality is a process control; the product records who
  verified what, but can't prove the evidence is genuine.
