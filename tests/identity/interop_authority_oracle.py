"""Executed certificates for every discovered todo-67/68 Python authority boundary."""

from __future__ import annotations

import secrets
from functools import partial
from typing import TYPE_CHECKING

from apps.core import integration
from apps.interop import ratelimit, tasks
from apps.interop.rnds_transport import FIXTURE_TRANSPORT
from apps.interop.webhook_transport import SYNTHETIC_TRANSPORT
from django.test import override_settings
from pytest_django.fixtures import SettingsWrapper

from interop import test_external_api, test_org_export, test_rnds_adapter, test_webhooks
from interop.support import PLATFORM_SECRETS, LocalBucket

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    import pytest

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph

API_CASES = (
    test_external_api.test_client_credentials_token_lists_appointments_with_opaque_ids,
    test_external_api.test_scopes_bound_every_resource_and_malformed_tokens_are_identical,
    test_external_api.test_foreign_and_unknown_external_ids_return_the_identical_404,
    test_external_api.test_revocation_rotation_and_approver_authority_take_effect_immediately,
)
DELIVERY_CASES = (
    test_external_api.test_writes_require_idempotency_keys_and_replay_exactly,
    test_webhooks.test_booking_is_signed_and_delivered_through_the_action_outbox,
    test_webhooks.test_lost_approver_authority_cancels_delivery_without_a_send,
)
COMPLETE = test_org_export.test_org_admin_export_is_complete_scoped_and_verifiable
HTTP = test_org_export.test_http_management_downloads_and_refuses_foreign_exports_alike
REDISPATCH = test_org_export.test_lost_handoffs_are_redispatched_by_reference_only
MISSING = test_org_export.test_missing_attachment_bytes_fail_the_export_without_parts
AUTHORITY = (
    test_org_export.test_export_needs_org_wide_authority_and_stops_once_it_is_lost
)
RNDS_CASES = (
    test_rnds_adapter.test_fixture_submission_is_sealed_sent_and_accepted,
    test_rnds_adapter.test_unknown_foreign_and_unauthorized_records_are_denied_alike,
)


def _provision(secret_root: Path) -> None:
    for name in PLATFORM_SECRETS:
        path = secret_root / f"{name}.secret"
        if not path.exists():
            path.write_text(secrets.token_hex(32), encoding="ascii")
            path.chmod(0o600)


def _delivered(
    case: Callable[[RbacGraph, Path, LocalBucket, None], None],
    subject: RbacGraph,
    secret_root: Path,
    bucket: LocalBucket,
) -> None:
    SYNTHETIC_TRANSPORT.reset()
    try:
        case(subject, secret_root, bucket, None)
    finally:
        SYNTHETIC_TRANSPORT.reset()


def _rnds(
    case: Callable[[RbacGraph, SettingsWrapper, list[UUID], str], None],
    subject: RbacGraph,
    patch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    """Run one RNDS oracle with its own settings, outbox capture and fixture."""
    dispatched: list[UUID] = []
    wrapper = SettingsWrapper()
    FIXTURE_TRANSPORT.reset()
    try:
        with patch.context() as local:
            local.setattr(integration, "_dispatch", dispatched.append)
            case(subject, wrapper, dispatched, database_url)
    finally:
        wrapper.finalize()
        FIXTURE_TRANSPORT.reset()


def run_interop_census(
    proofs: ExecutedProofs,
    graph: Callable[..., RbacGraph],
    patch: pytest.MonkeyPatch,
    secret_root: Path,
    database_url: str,
) -> None:
    """Observe real callable entry while the independent interop oracles run."""
    _provision(secret_root)
    objects = secret_root.parent / "interop-census-objects"
    with patch.context() as local:
        for case in API_CASES:
            bucket = LocalBucket()
            local.setattr(ratelimit, "_backend", bucket)
            proofs.run(case.__name__, partial(case, graph(), secret_root, bucket))
        for delivery in DELIVERY_CASES:
            bucket = LocalBucket()
            local.setattr(ratelimit, "_backend", bucket)
            proofs.run(
                delivery.__name__,
                partial(_delivered, delivery, graph(), secret_root, bucket),
            )
        sent: list[dict[str, object]] = []
        local.setattr(
            tasks.build_export, "apply_async", lambda **options: sent.append(options)
        )
        with override_settings(EHR_ATTACHMENT_ROOT=objects / "complete"):
            proofs.run(
                COMPLETE.__name__,
                partial(COMPLETE, graph(), objects / "complete", sent),
            )
        sent.clear()
        proofs.run(HTTP.__name__, partial(HTTP, graph(), sent))
        sent.clear()
        proofs.run(REDISPATCH.__name__, partial(REDISPATCH, graph(), sent))
        sent.clear()
        with override_settings(EHR_ATTACHMENT_ROOT=objects / "missing"):
            proofs.run(
                MISSING.__name__,
                partial(MISSING, graph(), objects / "missing", sent, database_url),
            )
        sent.clear()
        with patch.context() as revoking:
            proofs.run(
                AUTHORITY.__name__,
                partial(AUTHORITY, graph(), sent, database_url, revoking),
            )
    for rnds_case in RNDS_CASES:
        proofs.run(
            rnds_case.__name__,
            partial(_rnds, rnds_case, graph(), patch, database_url),
        )
