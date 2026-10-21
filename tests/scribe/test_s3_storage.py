"""Exercise the S3 wire contract with an in-repository authenticated fake."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import secrets
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest
from apps.core.secrets import FileSecretStore
from apps.ehr.attachment_storage import AttachmentStorageError, PendingUpload
from apps.scribe import s3_storage
from apps.scribe.s3_storage import (
    MAX_STORED_BYTES,
    Credentials,
    S3AttachmentStorage,
    configured_storage,
)
from django.db import transaction
from django.test import override_settings

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(repr=False)
class Response:
    """Return bounded response bytes like the standard HTTP client."""

    status: int
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def getheaders(self) -> list[tuple[str, str]]:
        return self.headers


@dataclass(repr=False)
class Wire:
    """Reject bad authentication before the fake object store sees a mutation."""

    credentials: Credentials
    objects: dict[str, bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    forced: Response | None = None

    def authenticate(
        self, method: str, target: str, body: bytes, headers: Mapping[str, str]
    ) -> None:
        algorithm, terms = headers["authorization"].split(" ", 1)
        values = dict(term.split("=", 1) for term in terms.split(","))
        credential, *scope = values["Credential"].split("/")
        names = values["SignedHeaders"].split(";")
        parsed = urlsplit(target)
        canonical = "\n".join(
            (
                method,
                parsed.path,
                parsed.query,
                "".join(f"{name}:{headers[name].strip()}\n" for name in names),
                ";".join(names),
                hashlib.sha256(body).hexdigest(),
            )
        )
        signed = "\n".join(
            (
                algorithm,
                headers["x-amz-date"],
                "/".join(scope),
                hashlib.sha256(canonical.encode()).hexdigest(),
            )
        )
        key = ("AWS4" + self.credentials.secret_key).encode()
        for part in scope:
            key = hmac.digest(key, part.encode(), "sha256")
        valid = (
            algorithm == "AWS4-HMAC-SHA256"
            and credential == self.credentials.access_key
            and scope[1:] == ["sa-east-1", "s3", "aws4_request"]
            and headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
            and hmac.compare_digest(
                values["Signature"], hmac.digest(key, signed.encode(), "sha256").hex()
            )
        )
        assert valid

    def request(
        self, method: str, target: str, body: bytes, headers: Mapping[str, str]
    ) -> Response:
        self.authenticate(method, target, body, headers)
        self.calls.append(method)
        if self.forced is not None:
            return self.forced
        parsed = urlsplit(target)
        key = parsed.path.removeprefix("/private-media/")
        if parsed.query == "list-type=2&prefix=pending%2F":
            result = Element("ListBucketResult")
            SubElement(result, "IsTruncated").text = "false"
            for name in sorted(self.objects):
                if name.startswith("pending/"):
                    SubElement(SubElement(result, "Contents"), "Key").text = name
            return Response(HTTPStatus.OK, tostring(result))
        if method == "DELETE":
            self.objects.pop(key, None)
            return Response(HTTPStatus.NO_CONTENT)
        if method == "PUT":
            return self.put(key, body, headers)
        if key not in self.objects:
            return Response(HTTPStatus.NOT_FOUND)
        data = self.objects[key]
        return Response(
            HTTPStatus.OK,
            data if method == "GET" else b"",
            [("ETag", hashlib.sha256(data).hexdigest())],
        )

    def put(self, key: str, body: bytes, headers: Mapping[str, str]) -> Response:
        if headers.get("if-none-match") == "*" and key in self.objects:
            return Response(HTTPStatus.PRECONDITION_FAILED)
        expected = headers.get("if-match")
        if expected is not None and (
            key not in self.objects
            or expected != hashlib.sha256(self.objects[key]).hexdigest()
        ):
            return Response(HTTPStatus.PRECONDITION_FAILED)
        self.objects[key] = body
        return Response(HTTPStatus.OK)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> tuple[S3AttachmentStorage, Wire]:
    credentials = Credentials(secrets.token_hex(16), secrets.token_hex(32))
    state = Wire(credentials)

    class Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.response = Response(HTTPStatus.INTERNAL_SERVER_ERROR)

        def request(
            self,
            method: str,
            target: str,
            body: bytes,
            headers: Mapping[str, str],
        ) -> None:
            self.response = state.request(method, target, body, headers)

        def getresponse(self) -> Response:
            return self.response

        def close(self) -> None:
            return None

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    backend = S3AttachmentStorage(
        endpoint="http://127.0.0.1:19040",
        bucket="private-media",
        region="sa-east-1",
        credentials=credentials,
        allow_loopback=True,
    )
    return backend, state


def test_signed_s3_custody_replays_and_conditional_replacement(
    wire: tuple[S3AttachmentStorage, Wire],
) -> None:
    storage, state = wire
    key = "a" * 64
    organization = str(uuid4())
    storage.mark_pending(key, organization)
    assert storage.pending_uploads() == [PendingUpload(key, organization)]
    storage.put(key, b"opaque-ciphertext")
    storage.put(key, b"opaque-ciphertext")
    assert storage.get(key) == b"opaque-ciphertext"
    with pytest.raises(AttachmentStorageError):
        storage.put(key, b"conflicting-ciphertext")
    assert state.objects[key] == b"opaque-ciphertext"
    storage.replace(key, b"replacement-ciphertext")
    assert storage.get(key) == b"replacement-ciphertext"
    storage.clear_pending(key)
    assert storage.pending_uploads() == []
    storage.delete(key)
    storage.delete(key)
    assert key not in state.objects


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (HTTPStatus.TEMPORARY_REDIRECT, b""),
        (HTTPStatus.OK, b"x" * (MAX_STORED_BYTES + 1)),
        (HTTPStatus.NOT_FOUND, b""),
    ],
)
def test_redirects_oversized_responses_and_absence_fail_closed(
    wire: tuple[S3AttachmentStorage, Wire],
    status: int,
    body: bytes,
) -> None:
    storage, state = wire
    state.forced = Response(status, body)
    with pytest.raises(AttachmentStorageError):
        storage.get("b" * 64)
    assert state.calls == ["GET"]


@pytest.mark.parametrize("key", ["../patient", "/", "g" * 64, "a" * 63])
def test_storage_keys_cannot_escape_the_private_namespace(
    wire: tuple[S3AttachmentStorage, Wire],
    key: str,
) -> None:
    storage, state = wire
    with pytest.raises(AttachmentStorageError):
        storage.get(key)
    assert state.calls == []


def test_oversized_put_never_starts_network_io(
    wire: tuple[S3AttachmentStorage, Wire],
) -> None:
    storage, state = wire
    with pytest.raises(AttachmentStorageError):
        storage.put("c" * 64, b"x" * (MAX_STORED_BYTES + 1))
    assert state.calls == []


@pytest.mark.django_db(transaction=True)
def test_external_io_cannot_run_in_a_tenant_transaction(
    wire: tuple[S3AttachmentStorage, Wire],
) -> None:
    storage, state = wire
    with transaction.atomic(), pytest.raises(AttachmentStorageError):
        storage.put("d" * 64, b"opaque-ciphertext")
    assert state.calls == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://s3.example.invalid",
        "https://127.0.0.1:19040",
        "https://s3.example.invalid/clinical-path",
        "https://s3.example.invalid?patient=selector",
        "https://s3.example.invalid#clinical-fragment",
    ],
)
def test_remote_configuration_is_tls_only_and_has_no_selector_channels(
    endpoint: str,
) -> None:
    with pytest.raises(AttachmentStorageError):
        S3AttachmentStorage(
            endpoint=endpoint,
            bucket="private-media",
            region="sa-east-1",
            credentials=Credentials(secrets.token_hex(16), secrets.token_hex(32)),
        )


def test_dev_minio_uses_only_loopback_and_explicit_secret_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("media-access-key", "media-secret-key"):
        path = tmp_path / f"{name}.secret"
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    monkeypatch.setattr(
        s3_storage, "secret_store", lambda: FileSecretStore(str(tmp_path))
    )
    with override_settings(
        SCRIBE_S3_ENDPOINT="http://127.0.0.1:19040",
        SCRIBE_S3_BUCKET="private-media",
    ):
        assert isinstance(configured_storage(dev=True), S3AttachmentStorage)
        with pytest.raises(AttachmentStorageError):
            configured_storage()
    with (
        override_settings(SCRIBE_S3_ENDPOINT="https://storage.example.invalid"),
        pytest.raises(AttachmentStorageError),
    ):
        configured_storage(dev=True)
