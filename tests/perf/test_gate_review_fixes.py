"""Behavioral checks for governed capture, bootstrap cleanup and HTTP sign-in."""

from __future__ import annotations

import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from importlib import import_module
from typing import TYPE_CHECKING, Self, cast
from unittest.mock import Mock

import pytest
from django.test import Client
from ops.perf import runtime

INVALID_OTP = "not-a-code"

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

    from ops.perf.locustfile import StaffUser


@pytest.mark.parametrize("failure", ["setup", "run", "none"])
def test_bootstrap_owns_secrets_and_cleans_up_on_every_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    sentinel = root / "retained"
    sentinel.write_text("owned sibling", encoding="ascii")
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL", "postgresql://owner@localhost:6543/test"
    )
    original = str(tmp_path / "original")
    monkeypatch.setenv("CLINIC_SECRET_DIR", original)
    setup = Mock(
        side_effect=RuntimeError("setup refused") if failure == "setup" else None
    )
    monkeypatch.setattr("django.setup", setup)
    operation = Mock(
        side_effect=RuntimeError("run refused") if failure == "run" else None
    )
    child = None
    expected = (
        pytest.raises(RuntimeError, match=f"{failure} refused")
        if failure != "none"
        else nullcontext()
    )
    with expected, runtime.bootstrap_django(root) as directory:
        child = directory
        assert directory.parent == root
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / "tenant-kek.secret").stat().st_mode & 0o777 == 0o600
        assert os.environ["CLINIC_SECRET_DIR"] == str(directory)
        operation()
    assert child is None or not child.exists()
    assert list(root.iterdir()) == [sentinel]
    assert os.environ["CLINIC_SECRET_DIR"] == original


@dataclass
class _Response:
    status_code: int
    text: str
    url: str
    failures: list[str] = field(default_factory=list)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        return None

    def failure(self, message: str) -> None:
        self.failures.append(message)


class _DjangoTransport:
    """Use the real CSRF/auth views; replace only the network transport."""

    def __init__(self, *, drop_csrf: str = "", bad_otp: bool = False) -> None:
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost")
        self.cookies: dict[str, str] = {}
        self.responses: list[_Response] = []
        self.drop_csrf = drop_csrf
        self.bad_otp = bad_otp
        self.tokens: list[str] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> _Response:
        values = dict(data or {})
        if method == "POST":
            self.tokens.append(values.get("csrfmiddlewaretoken", ""))
            if path == self.drop_csrf:
                values.pop("csrfmiddlewaretoken", None)
            if path == "/auth/verify/" and self.bad_otp:
                values["otp_token"] = INVALID_OTP
            response = self.client.post(
                path, values, HTTP_REFERER=(headers or {}).get("Referer", "")
            )
        else:
            response = self.client.get(
                path, follow=bool(kwargs.get("allow_redirects", False))
            )
            chain = getattr(response, "redirect_chain", [])
            if chain:
                path = chain[-1][0]
        self.cookies = {
            name: morsel.value for name, morsel in self.client.cookies.items()
        }
        result = _Response(
            response.status_code,
            response.content.decode(),
            "http://localhost" + path,
        )
        self.responses.append(result)
        return result

    def get(
        self, path: str, *, name: str = "", catch_response: bool = True
    ) -> _Response:
        return self.request("GET", path, allow_redirects=True)


@pytest.fixture
def staff_user(monkeypatch: pytest.MonkeyPatch) -> StaffUser:
    monkeypatch.setenv("LOCUST_SKIP_MONKEY_PATCH", "1")
    module = import_module("ops.perf.locustfile")
    monkeypatch.setattr(module.StaffUser, "host", "http://localhost")
    environment = import_module("locust.env").Environment(host="http://localhost")
    environment.runner = Mock()
    return cast("StaffUser", module.StaffUser(environment))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "failure", ["none", "receptionist", "password", "otp", "login_csrf", "otp_csrf"]
)
def test_locust_authenticates_with_real_csrf_and_fails_closed(
    staff_user: StaffUser,
    synthetic_secret_backend: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from apps.identity.models import User  # noqa: PLC0415
    from ops.perf import dataset  # noqa: PLC0415
    from ops.perf.profiles import PROFILES  # noqa: PLC0415
    from ops.perf.staff_http import provision_login  # noqa: PLC0415

    tenant = dataset.seed_tenant(PROFILES["P-small"], index=0)
    user_id = tenant.front_desk_id if failure == "receptionist" else tenant.admin_id
    login = provision_login(User.objects.get(pk=user_id))
    for name, value in {
        "PERF_CLINIC_ID": str(tenant.clinic_id),
        "PERF_USERNAME": login.username,
        "PERF_PASSWORD": "wrong" if failure == "password" else dataset.PASSWORD,
        "PERF_TOTP_SECRET_HEX": login.secret.hex(),
        "PERF_OTP_DEVICE": login.device,
    }.items():
        monkeypatch.setenv(name, value)
    if failure == "none":
        monkeypatch.delenv("PERF_OTP_DEVICE")
    if failure == "receptionist":
        monkeypatch.delenv("PERF_TOTP_SECRET_HEX")
    transport = _DjangoTransport(
        drop_csrf=(
            "/auth/login/"
            if failure == "login_csrf"
            else "/auth/verify/"
            if failure == "otp_csrf"
            else ""
        ),
        bad_otp=failure == "otp",
    )
    monkeypatch.setattr(staff_user, "client", transport)
    runner = cast("Mock", staff_user.environment.runner)
    if failure in {"none", "receptionist"}:
        with runtime.runtime_role():
            staff_user.on_start()
            staff_user.workspace()
        assert transport.responses[-1].status_code == 200
        assert not transport.responses[-1].url.startswith("http://localhost/auth/")
        assert len(transport.tokens) == (1 if failure == "receptionist" else 2)
        assert all(transport.tokens)
        if failure != "receptionist":
            assert transport.tokens[0] != transport.tokens[1]
        assert staff_user.environment.process_exit_code is None
        runner.quit.assert_not_called()
    else:
        stopped = import_module("locust.exception").StopUser
        with pytest.raises(stopped), runtime.runtime_role():
            staff_user.on_start()
        assert staff_user.environment.process_exit_code == 1
        runner.quit.assert_called_once()
        assert transport.responses[-1].failures
        if failure.endswith("csrf"):
            assert transport.responses[-1].status_code == 403


def test_locust_rejects_session_redirects_even_when_the_login_page_is_200(
    staff_user: StaffUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _Response(200, "login page", "http://localhost/auth/login/")
    monkeypatch.setattr(staff_user, "client", Mock(get=Mock(return_value=response)))
    with pytest.raises(import_module("locust.exception").StopUser):
        staff_user.workspace()
    assert response.failures
    assert staff_user.environment.process_exit_code == 1
