"""Typed internal UI API mounted at ``/api/ui/v1/``.

RPC-style POST views adapt service frozen dataclasses through plain
serializers; there are no ModelViewSets and record identifiers travel only in
request bodies. The committed ``docs/api/ui-v1.yaml`` is the contract.
"""
