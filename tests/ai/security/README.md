# AI and agent security corpus

All inputs are synthetic. Run the complete suite with
`uv run pytest --reuse-db tests/ai/security/`. Todos 54, 60 and 63 must rerun
this command before their commits; their capabilities must extend the matrix
when they are registered.

Each `corpus/*.json` has exactly `schema_version`, `data_class`, `id`, `surface`,
`language`, `text` and `model_patch`. Version 1 accepts only `synthetic`, the
five source surfaces and English/pt-BR. `model_patch` describes the malicious
action emitted by a fake model that deliberately obeys the source rather than
its trust label. Dynamic scope, policy and source identities come from a valid
real-role proposal. No prompt text is a security control.

Every source is tested as plain text, nested quoted JSON, base64 and URL-encoded
text against both registered agent capabilities. Keyword flagging is an
additional refusal, not the oracle: the base64 cases bypass it and must still
fail deterministic schemas or effect policy. Domain calls are observed with
wrapping spies, and authorized positive controls exercise both registered tools
and A1/A2/A3 execution. Reads and rendering use actual tenant roles and domain
services. Egress tests cover every provider protocol operation, exact positive
origins and hostile URL forms without any external network.

This suite proves the implemented synthetic boundaries, not a real model's
resistance, clinical performance, production egress-proxy deployment or an
external approval gate. `real_enabled` remains false. Corpus contents, prompts,
outputs, patient names and URLs must not be copied into runtime evidence;
receipts contain case ids, counts, hashes and closed outcomes only.
