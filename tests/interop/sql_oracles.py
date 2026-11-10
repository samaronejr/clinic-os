"""Executable authority protocols for the interop SQL resolvers."""

from interop.test_external_api import (
    test_foreign_and_unknown_external_ids_return_the_identical_404,
    test_revocation_rotation_and_approver_authority_take_effect_immediately,
    test_scopes_bound_every_resource_and_malformed_tokens_are_identical,
    test_token_endpoint_refuses_with_rfc6749_bodies,
    test_writes_require_idempotency_keys_and_replay_exactly,
)
from interop.test_org_export import (
    test_export_needs_org_wide_authority_and_stops_once_it_is_lost,
    test_http_management_downloads_and_refuses_foreign_exports_alike,
)
from interop.test_posture import (
    test_missing_foreign_and_clinic_scoped_contexts_expose_no_partner_rows,
)
from interop.test_rnds_adapter import (
    test_non_consented_and_revoked_records_are_refused,
    test_unknown_foreign_and_unauthorized_records_are_denied_alike,
)

CASES = {
    case.__name__: case
    for case in (
        test_foreign_and_unknown_external_ids_return_the_identical_404,
        test_revocation_rotation_and_approver_authority_take_effect_immediately,
        test_scopes_bound_every_resource_and_malformed_tokens_are_identical,
        test_token_endpoint_refuses_with_rfc6749_bodies,
        test_writes_require_idempotency_keys_and_replay_exactly,
        test_export_needs_org_wide_authority_and_stops_once_it_is_lost,
        test_http_management_downloads_and_refuses_foreign_exports_alike,
        test_missing_foreign_and_clinic_scoped_contexts_expose_no_partner_rows,
        test_non_consented_and_revoked_records_are_refused,
        test_unknown_foreign_and_unauthorized_records_are_denied_alike,
    )
}
SQL_PROTOCOL_ORACLES = {
    "clinic_app.interop_authority": (
        "test_export_needs_org_wide_authority_and_stops_once_it_is_lost",
        "test_revocation_rotation_and_approver_authority_take_effect_immediately",
    ),
    "clinic_app.interop_manager": (
        "test_missing_foreign_and_clinic_scoped_contexts_expose_no_partner_rows",
    ),
    "clinic_app.interop_api_session": (
        "test_revocation_rotation_and_approver_authority_take_effect_immediately",
        "test_scopes_bound_every_resource_and_malformed_tokens_are_identical",
    ),
    "clinic_app.interop_api_token": (
        "test_revocation_rotation_and_approver_authority_take_effect_immediately",
        "test_token_endpoint_refuses_with_rfc6749_bodies",
    ),
    "clinic_app.interop_api_read": (
        "test_foreign_and_unknown_external_ids_return_the_identical_404",
    ),
    "clinic_app.interop_api_write": (
        "test_writes_require_idempotency_keys_and_replay_exactly",
    ),
    "clinic_app.interop_manage": (
        "test_export_needs_org_wide_authority_and_stops_once_it_is_lost",
        "test_http_management_downloads_and_refuses_foreign_exports_alike",
    ),
    "clinic_app.interop_export_rows": (
        "test_export_needs_org_wide_authority_and_stops_once_it_is_lost",
    ),
    "clinic_app.interop_rnds_request": (
        "test_unknown_foreign_and_unauthorized_records_are_denied_alike",
        "test_non_consented_and_revoked_records_are_refused",
    ),
}
