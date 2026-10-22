"""Host-free inventory and guard-order checks complement real-role SQL probes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NoReturn, assert_never
from uuid import uuid4

import pytest
from apps.identity import (
    passkey_authentication,
    passkey_controls,
    passkey_views,
    passkeys,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import UserClinicRole
from apps.identity.passkey_challenges import Challenge, PasskeyDeniedError
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory
from webauthn.helpers import (
    bytes_to_base64url,
    parse_authentication_credential_json,
    parse_registration_credential_json,
)

from auth.passkey_test_support import SoftwareAuthenticator
from identity.legacy_sql_inventory import migration_definitions
from identity.test_permission_parity import INVENTORY
from identity.test_sql_guard_inventory import (
    assert_protocol_oracles,
    assert_staff_role_oracles,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.current_context import ClinicRoles

type GuardCase = Literal["assertion", "registration", "removal", "begin"]


def test_migration_functions_have_inventory_and_executable_classifications() -> None:
    # Given the independently scanned migration definitions and real registry.
    definitions = migration_definitions()
    entries = INVENTORY["sql_guards"]
    # When every source-defined function is checked without a database substitute.
    assert definitions.keys() <= entries.keys()
    for name, paths in definitions.items():
        assert entries[name]["discovery"]["migrations"] == paths, name
    # Then role and lifecycle labels retain their existing executable contracts.
    assert_staff_role_oracles(entries)
    assert_protocol_oracles(entries)


@pytest.mark.parametrize("case", ["assertion", "registration", "removal", "begin"])
def test_passkey_role_guard_denies_before_any_write(
    case: GuardCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a denying guard and pytest's real database blocker (no django_db mark).
    clinic = uuid4()
    observed: list[tuple[UUID, ClinicRoles]] = []

    def deny(*, clinic_id: UUID, roles: ClinicRoles) -> NoReturn:
        observed.append((clinic_id, roles))
        raise CurrentActorError

    module = {
        "assertion": passkey_authentication,
        "registration": passkeys,
        "removal": passkey_controls,
        "begin": passkey_views,
    }[case]
    monkeypatch.setattr(module, "require_current_actor_clinic_roles", deny)
    authenticator = SoftwareAuthenticator.create()
    challenge = Challenge(
        challenge=b"synthetic-guard-order",
        purpose="register",
        rp_id="testserver",
        origin="http://testserver",
        issued_at=0,
    )
    encoded = bytes_to_base64url(challenge.challenge)
    # When the actual callable is entered, even a DB read before denial fails.
    match case:
        case "assertion":
            assertion = parse_authentication_credential_json(
                authenticator.assertion(
                    challenge=encoded, user_handle=bytes_to_base64url(b"synthetic")
                )
            )
            with pytest.raises(PasskeyDeniedError):
                passkey_authentication.verify_assertion(
                    credential=assertion, challenge=challenge, clinic_id=clinic
                )
        case "registration":
            registration = parse_registration_credential_json(
                authenticator.registration(challenge=encoded, rp_id="testserver")
            )
            with pytest.raises(PasskeyDeniedError):
                passkeys.register_passkey(
                    clinic_id=clinic,
                    credential=registration,
                    challenge=challenge,
                    nickname="Synthetic guard order",
                    session_key="synthetic",
                )
        case "removal":
            with pytest.raises(PasskeyDeniedError):
                passkey_controls.remove_passkey(
                    clinic_id=clinic, passkey_id=uuid4(), session_key="synthetic"
                )
        case "begin":
            request = RequestFactory().post(
                "/auth/passkey/register/begin/", {"clinic_id": str(clinic)}
            )
            request.session = SessionStore()
            response = passkey_views.registration_begin(request)
            assert response.status_code == 403
            assert request.session.modified is False
        case unreachable:
            assert_never(unreachable)
    # Then it is this guard, not invalid fixture data, that refused the call.
    assert observed == [(clinic, tuple(UserClinicRole.Role))]
