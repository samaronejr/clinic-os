# Digest-bound agent execution

Todo 39 adds an execution platform, not real-provider authorization. No external
gate is satisfied here. The runnable external command is a labelled, offline
logistics reminder simulation. The other registered command creates an
operational review checklist through `workflows.create_task` in the approving
human's request. Neither command diagnoses, finalizes, prescribes, moves money
or sends a real patient message.

## Authority and approval

`AgentPolicyVersion` is append-only. An administrator with the canonical
`automation.admin` or `automation.organization` permission publishes its next
version. Each version binds the capability, autonomy ceiling, exact registered
argument schemas, daily action and simulated micro-BRL budgets, timeout,
administrative escalation queue and compensation strategy. Every version,
including a disabled version, supersedes queued work. A0 permits no write tools.
A1 is human-request drafting. A2 requires an explicit bounded policy. A3 requires
one or two distinct authorized approvals of the exact effect. A version naming
anything but an active V2 principal of its own clinic is refused with one
access denial, whether that principal is unknown, inactive or another clinic's.
Only that record-guard refusal becomes the denial; any other database error,
including another 42501, propagates unchanged.

V1 service principals retain availability-read-only grants. V2 principals and
their matching V2 `agent.execute` grants are separately owner-provisioned.
V1 identities are not promoted. Machines authenticate through the dedicated
`agent` database alias and `service_principal_context`; they never set a human
GUC and do not obtain generic staff, audit-table or key-table grants. The agent
alias must use a direct or session-pooled connection because send custody
survives transaction commits.

The action envelope is `action-payload-v1.json`. Canonicalization rejects
duplicate keys, floating-point values, non-finite numbers and unknown fields;
Unicode text is preserved exactly. The 17 effect fields, plus schema version
and action identity, are bound into SHA-256 of RFC 8785 bytes. The canonical
payload is stored in a purpose-bound encrypted field, not plaintext JSONB.
Recipients, amounts and documents cannot be introduced through a policy edit:
the shipped tools explicitly refuse those effects. Adding a clinical, monetary,
bulk or real-recipient tool requires a new reviewed domain command and its
validator; schema acceptance alone never authorizes it.

Set-valued arrays use ascending RFC 8785 encoded-byte order without duplicates.
For v1 authority memberships this compares `bundle_version` (restricted to 1
or 2), then canonical UUID `id`, then `role`. The SQL snapshot producer uses
that same order with bytewise text collation; payload admission rejects
reordered memberships rather than sorting them after the fact. Approval and
execution compare the exact canonical snapshot, including every current
membership and its bundle version. Clinical text is not normalized.

Approval requires actual recent TOTP verification. The server's challenge
receipt binds actor, clinic, digest and verification time. The approval binds
that receipt, the canonical authority snapshot and the full digest using
HMAC-SHA-256 with the dedicated `agent-attestation-v1` managed secret.
The key name is fixed in code; payloads cannot select a different key.
Approval, challenge, provider simulation and user-visible receipt have distinct
HMAC domains. These are server attestations, not qualified signatures.

## Execution and recovery

`apps/ai/execution.py` contains the AP algorithm once:

1. Authenticate the executor.
2. Claim execution custody for the immutable digest.
3. Acquire ordered mutation custody and reload current records.
4. Recompute the digest and verify approval attestations and distinct quorum.
5. Revalidate current authority, source publications, policy, provider, budgets
   and database time. Equality at `expires_at` is expired.
6. Reserve the budget and record the unique execution intent atomically.
7. Recheck at the send boundary, then dispatch outside the machine transaction.
8. Authenticate the outcome and persist its receipt against the current fence.

The initial synthetic implementation uses one bounded global send lock.
Database triggers on the relevant authority, policy, source and provider tables
take the matching transaction lock. A revocation therefore commits before the
final check, or waits for the send boundary to finish; it cannot commit in the
gap between revalidation and dispatch. This deliberately trades cross-clinic
throughput for a small, auditable lock order. A future finer-grained topology
must preserve the same tested invariant and cover every relevant mutation.

One digest has one execution and one stable provider idempotency key. A repeated
delivery never sends a committed intent again. An abandoned or ambiguous send
becomes `unknown`; `reconcile_action` queries the original key rather than
retrying. The offline simulator has a bounded process-local result cache.
After simulator process loss or eviction it honestly returns unknown, not an
invented successful receipt. A real provider adapter needs durable provider-side
lookup and its separate approval gates.

Known failure can enter `compensating`. Only the compensation policy already
bound into approval may run, under current authority. Uncertain cancellation,
revoked authority, or a manual/none strategy leaves `manual_required`.
Compensation history and receipts are retained; no record is hard-deleted.
The administrative queue exposes rejected, unknown and manual-required work.

## Provenance and privacy

`AgentRun` records the AI-10 contract, model/provider/version and prompt hash,
source references, consent basis, evaluation set, declared budget and timeout,
outcome, latency and simulated cost. Related proposal, approval, execution and
receipt rows provide the tool-call history. Reviewer edits have a separate
encrypted field. There is no reasoning or chain-of-thought field.

Transcripts, documents, messages, retrieved content and other agents' outputs
use `UntrustedContent`. Its model representation is a labelled data field, never
a tool instruction. Provenance retains origin/hash/flag metadata rather than
raw input text. Suspicious instructions are flagged; independent closed schemas,
permissions and tool registration still enforce every effect. No SQL, shell,
HTTP, filesystem, browser or cloud-administration command is registered.

Audit uses the existing fixed metadata vocabulary. Machine events record the
actual registered principal, not an impersonated approver. Content, destinations,
tokens and model input/output never enter audit, broker arguments or receipts.

## Schema and verification

The creating migration installs FORCE RLS, exact role grants, composite scope
foreign keys and immutable-term/state-machine guards on all seven new tenant
tables. Protected storage is encrypted from its first version. The migration is
irreversible; rollback is restore, not deletion of approval or execution history.

Focused acceptance lives in `tests/ai/test_agent_execution.py` and the action
payload tests. Repository authority/refusal catalogs, exact posture, schema
drift, audit catalog, live-catalog recovery membership, upgrade and restoration
remain required gates. Local synthetic evidence is L1 only; Root owns the stock
canonical grant and independent final review.
