"""Closed table posture and event vocabulary for enterprise governance."""

from typing import Final

TABLES: Final = (
    "identity_ssoconnection",
    "identity_scimtoken",
    "identity_scimresource",
    "identity_scimrolemapping",
    "identity_scimmembership",
    "identity_scimrolebinding",
    "identity_breakglassgrant",
    "identity_supportaccessgrant",
    "identity_governancenotice",
    "identity_unitgovernancechange",
)
RUNTIME_GRANTS: Final = {
    table: frozenset({"SELECT", "INSERT"})
    for table in TABLES
    if table != "identity_scimrolebinding"
} | {"identity_scimrolebinding": frozenset({"SELECT"})}
COLUMN_GRANTS: Final = frozenset(
    (table, column, "UPDATE")
    for table, columns in (
        ("identity_ssoconnection", ("revoked_at",)),
        ("identity_scimtoken", ("revoked_at",)),
        (
            "identity_scimresource",
            (
                "attributes",
                "search_hash",
                "active",
                "revision",
                "updated_at",
                "deleted_at",
            ),
        ),
        ("identity_scimrolemapping", ("revoked_at",)),
        ("identity_scimmembership", ("revoked_at",)),
        (
            "identity_breakglassgrant",
            ("revoked_at", "reviewed_at", "reviewed_by_id", "review_task_id"),
        ),
        ("identity_supportaccessgrant", ("revoked_at", "reviewed_at")),
    )
    for column in columns
)
EVENTS: Final = {
    "identity.sso.configured": ("identity.sso_connection", "configured"),
    "identity.sso.revoked": ("identity.sso_connection", "revoked"),
    "identity.sso.authenticated": ("identity.scim_resource", "authenticated"),
    "identity.scim.token_issued": ("identity.scim_token", "created"),
    "identity.scim.token_revoked": ("identity.scim_token", "revoked"),
    "identity.scim.mapping_approved": ("identity.scim_role_mapping", "approved"),
    "identity.scim.resource_created": ("identity.scim_resource", "created"),
    "identity.scim.resource_changed": ("identity.scim_resource", "changed"),
    "identity.scim.resource_revoked": ("identity.scim_resource", "revoked"),
    "identity.scim.binding_created": ("identity.scim_role_binding", "created"),
    "identity.scim.binding_revoked": ("identity.scim_role_binding", "revoked"),
    "identity.break_glass.granted": ("identity.break_glass_grant", "granted"),
    "identity.break_glass.read": ("identity.break_glass_grant", "read"),
    "identity.break_glass.revoked": ("identity.break_glass_grant", "revoked"),
    "identity.break_glass.reviewed": ("identity.break_glass_grant", "reviewed"),
    "identity.break_glass.notified": ("identity.governance_notice", "created"),
    "identity.support.approved": ("identity.support_access_grant", "approved"),
    "identity.support.read": ("identity.support_access_grant", "read"),
    "identity.support.revoked": ("identity.support_access_grant", "revoked"),
    "identity.unit_policy.changed": ("identity.unit_governance_change", "changed"),
    "identity.unit_template.propagated": (
        "identity.unit_governance_change",
        "propagated",
    ),
    "identity.credential.rotated": ("identity.unit_governance_change", "rotated"),
    "identity.recovery.slot_recorded": ("identity.unit_governance_change", "recorded"),
}
