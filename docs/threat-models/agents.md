# Threat model: agents, proposals and approvals

- Status: design baseline, 2026-09-24 (todo 2). Mitigations are planned work
  owned by the listed todos unless a current source path is named. The Tests
  column maps each mitigation id to the tests that prove it (todo 73,
  checked by `tests/infra/test_threat_model_coverage.py`). A mitigation that
  starts with **Proposal:** goes beyond the owning todo's plan text; that
  todo accepts or rejects it and isn't bound by it until then.
- Decisions: [ADR-008](../adr/ADR-008-workflows-and-approval-binding.md),
  [ADR-019](../adr/ADR-019-service-principals.md),
  [ADR-006](../adr/ADR-006-ai-gateway-routing.md)

## Scope

Agent capabilities that propose or perform actions: care coordination
(AI-05), front desk (AI-04), revenue cycle (AI-06) and analyst reminders
(AI-07). Covers `AgentPolicyVersion`, `AgentRun`, `ActionProposal`,
`Approval`, `ActionExecution`, receipts, compensation, the tool gateway and
the `clinic_agent` database role.

## Assets

- The authority to change clinical, scheduling and financial state.
- Canonical action payloads and their approval digests.
- Approver authority snapshots and step-up evidence.
- Untrusted content the agent reads: transcripts, documents, patient messages,
  knowledge base pages.

## Trust boundaries

1. Untrusted content into the model context.
2. Model output into the tool gateway (proposal validation).
3. Human approver to the approval service (session, step-up).
4. Executor (`clinic_agent` or approving human) to domain services and
   external providers.

## Data flow

```mermaid
flowchart LR
  subgraph Untrusted["Untrusted content"]
    Content["Transcripts, documents, messages, KB pages"]
  end
  subgraph AI["apps/ai"]
    Model["Model via gateway"]
    Validate["Schema and policy validation"]
    Proposal["ActionProposal with RFC 8785 digest"]
    Exec["Execute: reload, recompute digest, revalidate"]
  end
  subgraph Human["Staff browser"]
    Approver["Approver with step-up"]
  end
  subgraph Domain["Domain services"]
    Services["Registered domain commands"]
    PG[("PostgreSQL as clinic_agent, FORCE RLS")]
    Outbox["Outbox to providers"]
  end
  Content -- "data fields only" --> Model
  Model -- "typed tool call" --> Validate
  Validate --> Proposal
  Proposal --> Approver
  Approver -- "approve exact digest" --> Exec
  Exec --> Services
  Services --> PG
  Services --> Outbox
  Exec -- "receipt" --> PG
```

## Threats and mitigations

| ID | STRIDE | Threat | Mitigation | Todos | Tests |
| --- | --- | --- | --- | --- | --- |
| AA-S1 | Spoofing | An agent acts as a clinician. | Agents run as a `ServicePrincipal` on `clinic_agent`, mapped through `session_user`; a principal never sets `app.current_user_id`. | 7 | `tests/identity/test_service_principals.py::test_forged_human_context_never_authorizes_machine_reads`<br>`tests/identity/test_service_principals.py::test_machine_login_cannot_install_staff_context`<br>`tests/identity/test_service_principals.py::test_real_login_reads_only_exact_granted_clinic` |
| AA-S2 | Spoofing | A forged approval is recorded without a real approver. | Approval stores the approver authority snapshot, step-up evidence ref and a server attestation HMAC with a dedicated key. | 39 | `tests/ai/test_agent_guards.py::test_approval_attestation_replay_and_key_confusion_are_rejected`<br>`tests/ai/test_vote_attestation.py::test_sql_never_counts_a_vote_without_verified_step_up`<br>`tests/ai/test_agent_guards.py::test_fabricated_step_up_and_double_approval_cannot_add_votes` |
| AA-T1 | Tampering | The payload changes after approval. | Approval binds `SHA-256(RFC 8785(payload))`; execution recomputes the digest and refuses a mismatch; duplicate keys, floats, NaN and unknown fields are rejected. | 39 | `tests/ai/test_agent_guards.py::test_execution_refuses_an_approval_bound_to_another_digest`<br>`tests/ai/test_action_payload.py::test_complete_envelope_refuses_duplicate_keys`<br>`tests/ai/test_action_payload.py::test_complete_envelope_refuses_noninteger_json_numbers`<br>`tests/ai/test_action_payload.py::test_closed_fields_and_operation_arguments_are_enforced` |
| AA-T2 | Tampering | Instructions hidden in a document or message trigger a tool call (prompt injection). | Untrusted content is passed only as data fields; tool calls come from the model's typed output, are validated against schema and policy, and never parsed from content; an injection corpus runs against worst-case fake models. | 39, 49 | `tests/ai/security/test_agency.py::test_injection_following_model_cannot_acquire_an_effect`<br>`tests/ai/test_agent_execution.py::test_document_injection_is_flagged_and_never_calls_tools`<br>`tests/ai/test_units.py::test_model_data_retains_its_untrusted_origin_and_does_not_become_a_tool` |
| AA-T3 | Tampering | A proposal based on superseded sources executes. | Payload lists source versions and content hashes; execute-time revalidation rejects stale or superseded sources. | 39, 53 | `tests/ai/test_agent_execution.py::test_superseded_source_publication_blocks_execution`<br>`tests/ai/test_agent_guards.py::test_approval_refuses_a_payload_that_no_longer_matches_its_displayed_digest` |
| AA-R1 | Repudiation | Nobody can show who approved an effect or what happened. | One execution id per digest, persisted receipts, compensation records; provenance kept in `apps/ai` tables (D-18). | 39 | `tests/ai/test_agent_execution.py::test_concurrent_duplicate_executes_one_domain_effect`<br>`tests/ai/test_agent_execution.py::test_approve_fake_reminder_records_one_authenticated_receipt`<br>`tests/ai/test_agent_execution.py::test_known_failure_runs_bound_compensation_once` |
| AA-I1 | Information disclosure | An agent reads data outside its grant. | `clinic_agent` grants cover only tables listed per app; RLS helper `principal_has(perm, clinic)`; no SELECT on audit or key tables. | 7 | `tests/identity/test_service_principals.py::test_agent_cannot_read_ungranted_sensitive_tables`<br>`tests/identity/test_service_principals.py::test_machine_grants_reject_unsafe_permissions` |
| AA-I2 | Information disclosure | The agent exfiltrates data through a tool argument or URL. | No HTTP, shell, SQL, filesystem or browser tools; tool arguments follow closed schemas; egress allowlist. | 39, 49 | `tests/ai/security/test_egress.py::test_non_allowlisted_egress_never_reaches_a_network_operation`<br>`tests/ai/security/test_chart_isolation.py::test_worst_case_chart_model_cannot_return_tools_or_exfiltration`<br>`tests/ai/test_action_payload.py::test_closed_fields_and_operation_arguments_are_enforced` |
| AA-D1 | Denial of service | A runaway agent floods proposals or executions. | Per-policy budgets and timeouts, per-capability kill switch, tenant fairness on queues. | 9, 39 | `tests/ai/test_agent_guards.py::test_fairness_defers_only_the_exact_agent_task_and_tenant`<br>`tests/ai/security/test_budget_attacks.py::test_hostile_ceiling_and_repeated_prompts_cannot_override_organization_cap`<br>`tests/ai/test_agent_guards.py::test_successor_disabled_policy_revokes_queued_approval` |
| AA-E1 | Elevation of privilege | Excessive agency: the agent does more than the capability allows. | `AgentPolicyVersion` sets an autonomy ceiling A0 to A3 and a tool allowlist with argument schemas; prescribing and clinical finalization are never tools. | 39, 49 | `tests/ai/security/test_corpus.py::test_capability_matrix_cannot_silently_omit_a_new_registered_capability`<br>`tests/ai/security/test_agency.py::test_positive_control_each_supported_capability_and_autonomy_can_execute`<br>`tests/ai/test_agent_guards.py::test_bounded_policy_executes_without_fabricating_human_approval` |
| AA-E2 | Elevation of privilege | An approver whose authority was revoked still gets the action executed. | Execute-time revalidation of current authority with DB time; revocation after approval blocks execution. | 39 | `tests/ai/test_agent_execution.py::test_revoked_approver_blocks_after_approval`<br>`tests/ai/test_agent_native_refusals.py::test_agent_policy_refuses_authority_revoked_after_admission`<br>`tests/ai/test_agent_execution.py::test_expiry_equality_is_expired` |
| AA-E3 | Elevation of privilege | A worker dies after a remote effect and a retry doubles it. | Reservation before dispatch, same provider idempotency key on retry, ambiguous outcomes reconciled rather than resent. | 7, 39, 72 | `tests/ai/test_agent_execution.py::test_unknown_outcome_reconciles_same_key_without_retry`<br>`tests/ai/test_agent_guards.py::test_unknown_outcome_and_failed_compensation_require_manual_follow_up`<br>`tests/ai/test_agent_guards.py::test_send_boundary_revalidation_prevents_dispatch` |

## Residual risk and external gates

- Models may still produce wrong but schema-valid proposals; human review and
  evaluation (todo 47) are the controls, and live clinical use waits on EG-3.
- Front-desk actions with real patients wait on EG-2 and EG-5.
