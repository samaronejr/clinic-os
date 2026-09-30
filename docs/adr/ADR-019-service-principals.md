# ADR-019: Service principals on a dedicated clinic_agent role

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Threat model: [agents](../threat-models/agents.md)

## Context

Agents and workers will read and write tenant data without a human request.
The runtime role `clinic_app` is `NOBYPASSRLS`, and authority comes from
transaction GUCs (`app.current_user_id`, `app.current_tenant`). Setting
`app.current_user_id` to a clinician for machine work would make every agent
action look like that clinician's, and a shared role would give agents the
full runtime grant set.

## Decision

Service principals connect as a dedicated `clinic_agent` login.

- `clinic_agent` is `NOBYPASSRLS` and is mapped through `session_user` to a
  registered `ServicePrincipal`.
- `service_principal_context` authorizes the exact grant for the principal and
  clinic before setting tenant context.
- A principal never impersonates a clinician and never sets
  `app.current_user_id`.

## Consequences

- Machine actions are attributable to a named principal in audit and receipts.
- `clinic_agent` gets only the tables each app lists in its agent grants; it
  has no SELECT on audit or key tables.
- A new login role touches `ops/db/bootstrap.sql`, `ops/db/posture.py`, the
  resolver catalog and the Terraform role plan (H-4).
- Revoking a principal takes effect on the next context check.

## Rejected

- Reusing `clinic_app` with an actor GUC. Any code bug that sets the GUC
  wrong would give an agent a human's authority, and the database couldn't
  tell the two apart.

## Revisit trigger

None recorded. The AD table sets no trigger. Changing this model needs a new
ADR that supersedes this one.

## Owning todos

7. Consumers: 38, 39, 53, 54, 60.
