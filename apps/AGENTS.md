# Domain app contracts

Before changing a domain, read its `README.md`. Public services take explicit
keyword-only scope and derive actor authority from the current database context.

## Scribe

Read `scribe/README.md` before changing capture, receipt, encryption, or discard
behavior. The four public entrypoints are exported by `scribe/services.py`;
`tests/infra/test_module_boundaries.py` pins their complete signatures and defaults.
`tests/scribe/test_posture.py` pins every capture table's ENABLE/FORCE flags,
owner, policy role, command, and USING/WITH CHECK predicates. Run it together
with `tests/infra/test_new_app_posture.py` for exact table and column grants.

Keep the original upload deadline and immutable encounter/token/epoch binding.
Capture receipts acknowledge encrypted ingest, not completed object delivery.
The `scribe-capture` browser suite owns interruption, privacy, and accessibility
evidence. Physical devices and independent S3/MinIO qualification remain external.
