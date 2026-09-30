# ADR-009: Official messaging adapters and purpose-separated consent

- Status: accepted 2026-09-24
- Recorded by: todo 2

## Context

Clinics talk to patients over WhatsApp, email and SMS. Unofficial WhatsApp
automation risks number bans and breaks the platform's terms. The comms app
sends through the outbox with a fixed channel enum (`apps/comms/models.py`,
H-14), and consent purpose is limited by a DB check to teleconsultation today
(`apps/consent/models.py`, H-12). This plan treats care messages and marketing
as separate purposes; the legal basis for each is mapped under EG-2, not here.

## Decision

Messaging uses official provider adapters only: the Meta WhatsApp Cloud API or
an approved BSP, email and SMS.

- Consent is purpose-separated (transactional, care, marketing) through the
  consent taxonomy of todo 20.
- Adapters go through the outbox and the provider lifecycle registry.
- Inbound webhooks are authenticated on raw bytes before any scope lookup.

## Consequences

- Template approval, the 24 h customer-service window and per-template pricing
  become product rules the inbox must show.
- Marketing never rides a care thread, and a patient can refuse one purpose
  without losing the others.
- The channel enum is extended by migration rather than reusing an existing
  value.

## Rejected

- Unofficial WhatsApp automation. It violates the provider's terms, can get
  the clinic's number banned, and has no contract or DPA path under EG-5.

## Revisit trigger

None recorded. The AD table sets no trigger. Changing this model needs a new
ADR that supersedes this one.

## Owning todos

51. Consumers: 20, 54, 62.
