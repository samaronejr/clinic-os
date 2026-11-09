"""Real-role enterprise fixtures; only the IdP transport is synthetic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.identity.enterprise import configure_connection, issue_scim_token
from apps.identity.models import SsoConnection, User, UserClinicRole
from apps.providers.models import CapabilityVersion, ProviderCapability
from django.test import Client

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context, permission_context
from otp_test_support import create_totp_device, fixed_otp_time, runtime_role, token_for

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class EnterpriseSetup:
    connection: SsoConnection
    client: Client
    token: str

    @property
    def session_key(self) -> str:
        return session_key(self.client)


def enterprise_setup(graph: RbacGraph) -> EnterpriseSetup:
    with owner_context(graph.organization_a):
        for clinic in (graph.clinic_a, graph.clinic_b):
            UserClinicRole.objects.get_or_create(
                organization_id=graph.organization_a,
                clinic_id=clinic,
                user_id=graph.clinic_admin,
                role="org_admin",
            )
        capability, _created = ProviderCapability.objects.get_or_create(
            key="enterprise_identity", clinic_id=None
        )
        # The platform capability is shared: graphs seeded in one test (the
        # cut1 census runs several enterprise oracles) reuse its one version.
        version, _created = CapabilityVersion.objects.get_or_create(
            capability=capability,
            provider="deterministic-oidc",
            environment="synthetic",
            api_version="oidc-1.0-scim-2.0",
            defaults={"state": "selected_in_plan"},
        )
        capability.current_version = version
        capability.save(update_fields=("current_version",))
    client = verified_client(graph)
    with permission_context(graph, graph.clinic_admin):
        configured = configure_connection(
            clinic_id=graph.clinic_a,
            issuer="https://synthetic-idp.invalid",
            client_id="synthetic-clinic",
            session_key=session_key(client),
        )
        token = issue_scim_token(
            clinic_id=graph.clinic_a,
            connection_id=configured.pk,
            session_key=session_key(client),
        )
    return EnterpriseSetup(configured, client, token)


def verified_actor_client(actor_id: UUID) -> Client:
    """Use an actual confirmed device and framework session, never a freshness mock."""
    user = User.objects.get(pk=actor_id)
    device = create_totp_device(actor_id, confirmed=True)
    client = Client()
    with runtime_role():
        client.force_login(user)
        with fixed_otp_time():
            response = client.post(
                "/auth/verify/",
                {
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                    "next": "/auth/protected/",
                },
            )
    assert response.status_code == 302
    return client
