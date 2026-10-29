# Synthetic signing PKI

`signature-pki.json` was generated once by the locked environment:

```sh
uv run --frozen --no-sync --no-env-file python \
  tests/prescription/fixtures/generate_signature_pki.py
```

The script emits the complete JSON to stdout. Replacing the fixture rotates
all synthetic keys, certificates, CRLs and vectors together; tests never
regenerate it. Dates and serials are fixed around `2036-01-01T00:00:00Z`, and
the test clocks explicitly use that instant. Actual cryptographic signatures
are checked; no positive trust verdict is stored.

The public test private keys are intentionally not secrets or credentials.
They belong only to synthetic signing/timestamp leaves. Root and intermediate
private keys are never emitted. Do not trust this root or these keys live.

The test vectors are encoded independently of the application signer. They
include a real valid signature, a revoked leaf listed in an issuer-signed CRL,
an expired leaf, an intermediate with the correct name but wrong key, and a
corrupted timestamp signature. A second root with the same subject but a
different key proves that issuer-name matching cannot establish trust.
