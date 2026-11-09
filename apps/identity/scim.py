"""Clinic-scoped directory commands; plaintext attributes never enter SQL selectors."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.identity.enterprise_authority import GovernanceDeniedError
from apps.identity.models import ScimMembership, ScimResource, SsoConnection
from apps.identity.scim_context import scim_context
from apps.identity.scim_paths import scim_resource_path
from apps.identity.scim_schema import (
    SCHEMAS,
    ScimConflictError,
    ScimInputError,
    blind_index,
    equality_filter,
)
from apps.identity.scim_schema import (
    attributes as parse_attributes,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = (
    "ScimConflictError",
    "ScimInputError",
    "create_resource",
    "resource_document",
    "scim_context",
    "search_resources",
    "update_resource",
)
MAX_SEARCH_INDEX = 100000
MAX_SEARCH_COUNT = 100


def _connection(*, clinic_id: UUID) -> SsoConnection:
    target = SsoConnection.objects.filter(
        clinic_id=clinic_id, revoked_at__isnull=True
    ).first()
    if target is None:
        raise GovernanceDeniedError
    return target


def _members(resource: ScimResource, values: Mapping[str, object]) -> None:
    if resource.kind != "Groups":
        return
    members = values.get("members", [])
    if not isinstance(members, list):
        raise ScimInputError
    desired = {UUID(member["value"]) for member in members}
    actual = set(
        ScimResource.objects.filter(
            pk__in=desired,
            kind="Users",
            connection_id=resource.connection_id,
            deleted_at__isnull=True,
        ).values_list("pk", flat=True)
    )
    if actual != desired:
        raise GovernanceDeniedError
    current = ScimMembership.objects.filter(group=resource, revoked_at__isnull=True)
    current.exclude(member_id__in=desired).update(revoked_at=timezone.now())
    existing = set(current.values_list("member_id", flat=True))
    for member_id in sorted(desired - existing):
        ScimMembership.objects.create(
            organization_id=resource.organization_id,
            clinic_id=resource.clinic_id,
            group=resource,
            member_id=member_id,
        )


def _sync(resource: ScimResource) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.enterprise_sync_directory(%s)",
            [resource.connection_id],
        )
        changes = cursor.fetchall()
    for binding_id, clinic_id, verb in changes:
        record_phase1_event(
            f"identity.scim.binding_{verb}",
            clinic_id=clinic_id,
            affected_record_id=binding_id,
        )


def create_resource(
    *, clinic_id: UUID, kind: str, attributes: Mapping[str, object]
) -> ScimResource:
    """Provision a separate account; names and emails never link existing users."""
    values = parse_attributes(kind=kind, value=attributes)
    with transaction.atomic():
        target = _connection(clinic_id=clinic_id)
        user_id = None
        if kind == "Users":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.enterprise_new_directory_user(%s)", [target.pk]
                )
                row = cursor.fetchone()
            if row is None or row[0] is None:
                raise GovernanceDeniedError
            user_id = row[0]
        external = values.get("externalId")
        label = values["userName" if kind == "Users" else "displayName"]
        try:
            result = ScimResource.objects.create(
                clinic_id=clinic_id,
                organization_id=target.organization_id,
                connection=target,
                kind=kind,
                user_id=user_id,
                external_hash=(
                    blind_index(
                        connection_id=target.pk, field="externalId", value=external
                    )
                    if isinstance(external, str)
                    else None
                ),
                search_hash=blind_index(
                    connection_id=target.pk,
                    field="userName" if kind == "Users" else "displayName",
                    value=str(label),
                ),
                attributes=values,
                active=values.get("active", True) is True,
            )
        except IntegrityError as error:
            if getattr(
                getattr(error.__cause__, "diag", None), "constraint_name", ""
            ) not in {
                "identity_scim_external_unique",
                "identity_scim_username_current",
            }:
                raise
            raise ScimConflictError from error
        _members(result, values)
        _sync(result)
        record_phase1_event(
            "identity.scim.resource_created",
            clinic_id=clinic_id,
            affected_record_id=result.pk,
        )
        return result


def resource_document(
    *, clinic_id: UUID, resource_id: UUID, kind: str
) -> dict[str, object]:
    """Expose only the caller's current directory resource and its opaque SCIM URL."""
    target = _connection(clinic_id=clinic_id)
    resource = ScimResource.objects.filter(
        pk=resource_id,
        clinic_id=clinic_id,
        connection=target,
        kind=kind,
        deleted_at__isnull=True,
    ).first()
    if resource is None:
        raise GovernanceDeniedError
    return {
        **resource.attributes,
        "schemas": [SCHEMAS[kind]],
        "id": str(resource.pk),
        "meta": {
            "resourceType": "User" if kind == "Users" else "Group",
            "version": f'W/"{resource.revision}"',
            "location": scim_resource_path(kind=kind, resource_id=resource.pk),
        },
    }


def update_resource(
    *,
    clinic_id: UUID,
    resource_id: UUID,
    values: Mapping[str, object],
    expected_revision: int | None = None,
    delete: bool = False,
) -> ScimResource:
    """Keep CAS revisions, subjects and account bindings on retained resources."""
    with transaction.atomic():
        target = _connection(clinic_id=clinic_id)
        resource = (
            ScimResource.objects.select_for_update()
            .filter(
                pk=resource_id,
                clinic_id=clinic_id,
                connection=target,
                deleted_at__isnull=True,
            )
            .first()
        )
        if resource is None:
            raise GovernanceDeniedError
        kind = resource.kind
        parsed = parse_attributes(kind=kind, value=values)
        if expected_revision is not None and resource.revision != expected_revision:
            raise ScimConflictError
        if parsed.get("externalId") != resource.attributes.get("externalId"):
            raise ScimInputError
        if not delete and parsed == resource.attributes:
            return resource
        resource.attributes = parsed
        resource.active = not delete and parsed.get("active", True) is True
        resource.deleted_at = timezone.now() if delete else None
        resource.search_hash = blind_index(
            connection_id=target.pk,
            field="userName" if kind == "Users" else "displayName",
            value=str(parsed["userName" if kind == "Users" else "displayName"]),
        )
        resource.revision += 1
        resource.save(
            update_fields=(
                "attributes",
                "active",
                "deleted_at",
                "search_hash",
                "revision",
                "updated_at",
            )
        )
        _members(resource, parsed)
        _sync(resource)
        record_phase1_event(
            "identity.scim.resource_revoked"
            if delete
            else "identity.scim.resource_changed",
            clinic_id=clinic_id,
            affected_record_id=resource.pk,
        )
        return resource


def search_resources(
    *,
    clinic_id: UUID,
    kind: str,
    filter_text: str = "",
    start_index: int = 1,
    count: int = 100,
) -> dict[str, object]:
    """Bound pagination and execute exact equality only through HMAC indexes."""
    if (
        kind not in SCHEMAS
        or not 1 <= start_index <= MAX_SEARCH_INDEX
        or not 0 <= count <= MAX_SEARCH_COUNT
    ):
        raise ScimInputError
    target = _connection(clinic_id=clinic_id)
    rows = ScimResource.objects.filter(
        connection=target, kind=kind, deleted_at__isnull=True
    )
    if filter_text:
        field, value = equality_filter(filter_text)
        if field not in {
            "externalId",
            "userName" if kind == "Users" else "displayName",
        }:
            raise ScimInputError
        column = "external_hash" if field == "externalId" else "search_hash"
        rows = rows.filter(
            **{column: blind_index(connection_id=target.pk, field=field, value=value)}
        )
    total = rows.count()
    identifiers = rows.order_by("id").values_list("id", flat=True)[
        start_index - 1 : start_index - 1 + count
    ]
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": count,
        "Resources": [
            resource_document(clinic_id=clinic_id, resource_id=identifier, kind=kind)
            for identifier in identifiers
        ],
    }
