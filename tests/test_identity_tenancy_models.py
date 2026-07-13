from typing import Final
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.tenancy.models import TenantProbe, TenantScopedModel
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, models, transaction
from psycopg.errors import ForeignKeyViolation

COMPOSITE_CONSTRAINT_NAME: Final = "identity_userclinicrole_org_clinic_fk"
SET_COMPOSITE_CONSTRAINTS_IMMEDIATE_SQL: Final = (
    'SET CONSTRAINTS "identity_userclinicrole_org_clinic_fk" IMMEDIATE'
)


def _set_local_tenant(organization_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )


def test_identity_user_is_the_project_auth_model() -> None:
    # Given: the project settings loaded by pytest-django
    # When: Django's configured authentication model is inspected
    configured_auth_model = settings.AUTH_USER_MODEL

    # Then: the identity domain owns the project user model
    assert configured_auth_model == "identity.User"


def test_django_registry_selects_the_identity_user_model() -> None:
    # Given: Django has initialized the identity application
    # When: the swappable user model and identity registry entry are resolved
    selected_user_model = get_user_model()
    registered_user_model = django_apps.get_model("identity", "User")

    # Then: both public lookup paths resolve the custom identity model
    assert selected_user_model is User
    assert registered_user_model is User


@pytest.mark.django_db(transaction=True)
def test_identity_models_generate_uuid_primary_keys() -> None:
    # Given: a wholly synthetic identity graph
    organization_id = uuid4()
    with transaction.atomic():
        _set_local_tenant(organization_id)
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Organization Alpha",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            organization=organization,
            name="Synthetic Clinic Alpha",
            crm_uf="SP",
        )
        user = User.objects.create_user(username="synthetic-user-alpha")

        # When: a role is created through the ordinary ORM surface
        role = UserClinicRole.objects.create(
            user=user,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.OWNER,
        )

    # Then: every identity aggregate uses a UUID primary key
    assert all(
        isinstance(instance.pk, UUID) for instance in (organization, clinic, user, role)
    )


def test_identity_user_defines_no_domain_role_flags() -> None:
    # Given: all concrete fields on the custom user model
    # When: field names are collected from model metadata
    field_names = {field.name for field in User._meta.get_fields()}

    # Then: clinic roles are not duplicated as user fields
    assert field_names.isdisjoint(
        {"role", "is_owner", "is_physician", "is_receptionist", "is_clinic_admin"}
    )


def test_tenant_scoped_model_is_abstract_and_probe_is_concrete() -> None:
    # Given: the tenancy base model and its internal probe implementation
    # When: Django model metadata and the inherited tenant field are inspected
    organization_field = TenantProbe._meta.get_field("organization")

    # Then: only the probe is materialized and it points to Organization
    assert TenantScopedModel._meta.abstract is True
    assert TenantProbe._meta.abstract is False
    assert organization_field.related_model is Organization


def test_user_clinic_role_values_are_canonical() -> None:
    # Given: the single role authority's TextChoices declaration
    # When: its stored values are read in declaration order
    stored_values = tuple(UserClinicRole.Role.values)

    # Then: only the approved canonical values exist
    assert stored_values == (
        "owner",
        "physician",
        "receptionist",
        "clinic_admin",
    )


def test_clinic_declares_the_composite_foreign_key_target() -> None:
    # Given: the Clinic model's named constraints
    # When: the composite target constraint is selected by its stable name
    target_constraint = next(
        constraint
        for constraint in Clinic._meta.constraints
        if constraint.name == "identity_clinic_org_id_uniq"
    )

    # Then: organization and id form the exact unique target
    assert isinstance(target_constraint, models.UniqueConstraint)
    assert target_constraint.fields == ("organization", "id")


@pytest.mark.django_db(transaction=True)
def test_database_has_the_validated_composite_foreign_key() -> None:
    # Given: the migrated PostgreSQL catalog
    # When: the exact UserClinicRole composite constraint is inspected
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                constraint_row.convalidated,
                constraint_row.condeferrable,
                constraint_row.condeferred,
                ARRAY(
                    SELECT source_attribute.attname
                    FROM unnest(constraint_row.conkey) WITH ORDINALITY
                        AS source_key(attnum, position)
                    JOIN pg_attribute AS source_attribute
                        ON source_attribute.attrelid = constraint_row.conrelid
                        AND source_attribute.attnum = source_key.attnum
                    ORDER BY source_key.position
                ),
                ARRAY(
                    SELECT target_attribute.attname
                    FROM unnest(constraint_row.confkey) WITH ORDINALITY
                        AS target_key(attnum, position)
                    JOIN pg_attribute AS target_attribute
                        ON target_attribute.attrelid = constraint_row.confrelid
                        AND target_attribute.attnum = target_key.attnum
                    ORDER BY target_key.position
                ),
                constraint_row.confrelid::regclass::text
            FROM pg_constraint AS constraint_row
            WHERE constraint_row.conrelid = 'identity_userclinicrole'::regclass
                AND constraint_row.conname = %s
                AND constraint_row.contype = 'f'
            """,
            [COMPOSITE_CONSTRAINT_NAME],
        )
        constraint = cursor.fetchone()

    # Then: it is validated and enforces the exact ordered column pairs
    assert constraint == (
        True,
        True,
        True,
        ["organization_id", "clinic_id"],
        ["organization_id", "id"],
        "identity_clinic",
    )


@pytest.mark.django_db(transaction=True)
def test_same_organization_role_satisfies_the_composite_foreign_key() -> None:
    # Given: a synthetic user, organization, and clinic in that organization
    organization_id = uuid4()
    with transaction.atomic():
        _set_local_tenant(organization_id)
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Organization Control",
            cnpj="11111111111111",
        )
        clinic = Clinic.objects.create(
            organization=organization,
            name="Synthetic Clinic Control",
            crm_uf="RJ",
        )
        user = User.objects.create_user(username="synthetic-user-control")

        # When: a same-organization role is inserted and checks are forced
        role = UserClinicRole.objects.create(
            user=user,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.PHYSICIAN,
        )
        with connection.cursor() as cursor:
            cursor.execute(SET_COMPOSITE_CONSTRAINTS_IMMEDIATE_SQL)

    # Then: the accepted role retains the matching organization and clinic
    assert role.organization_id == clinic.organization_id


@pytest.mark.django_db(transaction=True)
def test_composite_foreign_key_rejects_reassigning_role_organization(
    superuser_database_url: str,
) -> None:
    # Given: a valid synthetic role in organization A and a separate organization B
    organization_a_id = uuid4()
    organization_b_id = uuid4()
    user = User.objects.create_user(username="synthetic-user-mismatch")
    with transaction.atomic():
        _set_local_tenant(organization_a_id)
        organization_a = Organization.objects.create(
            id=organization_a_id,
            name="Synthetic Organization A",
            cnpj="22222222222222",
        )
        clinic_a = Clinic.objects.create(
            organization=organization_a,
            name="Synthetic Clinic A",
            crm_uf="MG",
        )
        role = UserClinicRole.objects.create(
            user=user,
            organization=organization_a,
            clinic=clinic_a,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    with transaction.atomic():
        _set_local_tenant(organization_b_id)
        organization_b = Organization.objects.create(
            id=organization_b_id,
            name="Synthetic Organization B",
            cnpj="33333333333333",
        )

    # When: only its organization is reassigned and the deferred check is forced
    with psycopg.connect(superuser_database_url) as superuser_connection:
        superuser_connection.execute("SET LOCAL search_path = clinic_app, pg_catalog")
        superuser_connection.execute(
            """
            UPDATE clinic_app.identity_userclinicrole
            SET organization_id = %s
            WHERE id = %s
            """,
            (organization_b.pk, role.pk),
        )
        with pytest.raises(ForeignKeyViolation) as exc_info:
            superuser_connection.execute(SET_COMPOSITE_CONSTRAINTS_IMMEDIATE_SQL)
        superuser_connection.rollback()

    # Then: PostgreSQL names the intended composite constraint as the rejector
    assert exc_info.value.diag.constraint_name == COMPOSITE_CONSTRAINT_NAME
