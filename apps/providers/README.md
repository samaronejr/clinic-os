# apps/providers - provider capability lifecycle registry

**Binding contract.** This app owns the database-of-record for every
external provider capability. It replaces the permanent
`real_enabled=False` constants: live code paths ask
`apps.providers.services.is_live` and the answer is honest state, not a
hardcoded denial.

## Tables (all platform-level, owner-written, `clinic_app` SELECT-only)

| Table | Contents |
| --- | --- |
| `providers_providercapability` | one row per capability key; `clinic_id NULL` = platform default, non-null = per-clinic override that wins for that clinic; `current_version` points at the governing version |
| `providers_capabilityversion` | provider/account/environment/api_version/region/retention_terms + `state`; the state machine lives here |
| `providers_capabilityapproval` | immutable owner decisions (approver name/role free text, evidence URI, decided_at) |
| `providers_activationrecord` | one row per transition that reached `activated` |
| `providers_healthevent` | operator-recorded degrade/revoke notes |

## State machine (DB trigger `providers_version_transition_v1`)

```
researched -> selected_in_plan -> approved_to_test -> sandbox
    -> production_authorized -> activated <-> degraded
any non-terminal state -> revoked (terminal)
```

Illegal transitions raise SQLSTATE `P0001` (`provider_transition_denied`).
Versions in `approved_to_test` or beyond carry an immutable `approval_id`;
the pairing is enforced by CHECK plus trigger. Approvals, activation
records and health events are immutable; nothing is hard-deleted.

## The runtime gate

```python
is_live(key, *, clinic_id, environment=None) -> bool
current_version(key, *, clinic_id=None) -> CapabilityVersion | None
```

`is_live` is True only when ALL THREE hold:

1. the current version's state is `activated`;
2. the resolved process data mode is `live` (`settings.CLINIC_DATA_MODE`,
   or the injected `environment` mapping in unit tests);
3. `ops.release.activation.require_live_runtime` does not raise.

Condition 2 is mandatory and not implied by 3: `require_live_runtime` is a
no-op outside live mode, so an `activated` row alone never reports live in
the synthetic suite. The mode check runs first, so the gate never touches
the database outside live mode. Unknown keys, unknown scopes and malformed
arguments fail closed (`False`).

## Owner CLI

```
uv run python manage.py provider_capability propose --key <k> --provider <p> [--state researched|selected_in_plan] ...
uv run python manage.py provider_capability approve|activate|degrade|revoke --key <k> --approver-name <n> --approver-role <r> --evidence-uri <u> [--reason ...]
uv run python manage.py provider_capability report [--markdown]
```

Runs as `clinic_owner` only (`assert_owner_database_role`); `activate`
walks `approved_to_test -> sandbox -> production_authorized -> activated`
in one transaction under a fresh approval record, and re-activates from
`degraded` directly. Every transition appends a registered system-chain
audit event (`providers.capability.*`).

## Capability keys

Keys are the record-set names in `docs/integrations/records/2026-09-24-v2/`
(`email`, `sms`, `whatsapp`, `video`, `physician_registration`,
`pdf_rendering`, `qualified_signing`, `signature_verification`, `pix`,
`psp_card`, `attachment_storage`, `attachment_scanning`, `hosted_pitr`,
`data_at_rest`, `tenant_key_management`, `managed_secrets`,
`tls_transport`, `asr`, `llm_inference`, `sncr`, `rnds`, `tiss`, `nfse`,
`object_storage_media`). The plan's informal names map: `psp_pix` = `pix`,
`icp_signing` = `qualified_signing`, `physician_registry` =
`physician_registration`, `pdf_render` = `pdf_rendering`, `object_storage`
= `object_storage_media`, `malware_scan` = `attachment_scanning`, `kms` =
`tenant_key_management`.

## Seed

Migration `0001` inserts every capability at `researched` plus the PV
table's proposed primaries/alternatives as `selected_in_plan` versions
(D-12). Transactional tests flush the seed; rebuild it with
`apps.providers.migrations._seed_v2.seed_v2_capabilities(django.apps.apps)`
or the `tests/provider_gate_support.py` helpers.

## Never

- No `clinic_app` writes: the runtime role holds SELECT only.
- No auto-transitions, no treating `sandbox` as live, no removing the
  synthetic adapters.
- `is_live` is not an approval: `activated` only makes a capability
  eligible for the live runtime contract.
