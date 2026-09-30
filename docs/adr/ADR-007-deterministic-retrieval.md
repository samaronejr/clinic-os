# ADR-007: Deterministic patient-scoped retrieval, no vector store at start

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Related decisions: D-15

## Context

The pre-visit brief and chart assistant (AI-02) and the front-desk agent's
knowledge base (AI-04) need context for the model. Chart data is tenant-scoped
by RLS and much of it is encrypted with envelope keys, so it can't be indexed
by an outside search service without creating a plaintext copy. Every read
must respect the same permissions as a human read (ADR-003).

## Decision

Context is assembled deterministically, per patient, through domain services.

- Assembly calls the same services and permission checks that the chart UI
  uses; restricted records stay out without a grant.
- There's no vector store at start (D-15).
- The AI-04 knowledge base uses deterministic catalog and keyword lookup.

## Consequences

- A retrieval can't return another patient's or tenant's data unless the
  service layer itself is wrong, and that layer is already tested for
  identical denial.
- Context size is bounded by what the services return, so large charts need
  summarization rules inside the budget.
- Citation checks are simple: every cited source is a record reference the
  assembler returned.

## Rejected

- A cross-tenant vector index. It creates a shared store of clinical text
  outside RLS.
- pgvector now. It adds embeddings of encrypted content with no measured
  need yet.

## Revisit trigger

Chart context exceeds the model budget in more than 5% of eval cases, or KB
keyword recall falls below 90% on the KB eval set.

## Owning todos

44. Consumers: 46, 54.
