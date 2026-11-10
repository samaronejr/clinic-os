"""Versioned external partner API mounted at ``/api/v1/``.

Separate from the first-party ``/api/ui/v1/``: OAuth 2.0 client credentials,
bearer tokens, closed scopes, opaque external ids in paths, a Redis token bucket
per client and ``Idempotency-Key`` on every write. ``docs/api/external-v1.yaml``
is the committed contract.
"""
