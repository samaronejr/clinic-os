"""Bounded private S3-compatible transport; no real calls without lifecycle gates.

AWS Signature V4, conditional PUT and TLS use the standard library so the
approved dependency set does not grow. The synthetic default is the in-repo
filesystem fake, not S3. Only explicit dev MinIO or an activated live capability
may construct the configured network adapter; redirects are never followed.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import re
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET

from django.conf import settings
from django.db import connection

from apps.core.secrets import secret_store
from apps.ehr.attachment_storage import (
    KEY_PATTERN,
    AttachmentStorageError,
    PendingUpload,
)
from apps.scribe.contracts import MAX_CHUNK_BYTES

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_STORED_BYTES = MAX_CHUNK_BYTES + 8192
UUID_TEXT_LENGTH = 36


@dataclass(frozen=True, slots=True)
class Credentials:
    """Credentials never appear in repr, receipts, exceptions or evidence."""

    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


class S3AttachmentStorage:
    """Private path-style S3 with write-once objects and durable pending receipts."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        credentials: Credentials,
        allow_loopback: bool = False,
    ) -> None:
        """Allow plaintext HTTP only for an explicitly selected local dev/fake."""
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        local = hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not hostname
            or re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) is None
            or re.fullmatch(r"[a-z0-9-]{1,32}", region) is None
            or (
                parsed.scheme != "https"
                and not (allow_loopback and local and parsed.scheme == "http")
            )
            or (local and not allow_loopback)
        ):
            raise AttachmentStorageError
        self._endpoint = parsed
        self._hostname = hostname
        self._bucket = bucket
        self._region = region
        self._credentials = credentials

    def _key(self, key: str) -> str:
        if KEY_PATTERN.fullmatch(key) is None:
            raise AttachmentStorageError
        return key

    def _request(
        self,
        method: str,
        key: str = "",
        *,
        data: bytes = b"",
        query: str = "",
        conditions: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """Bound both socket lifetime and response bytes; reject redirects."""
        if len(data) > MAX_STORED_BYTES or connection.in_atomic_block:
            raise AttachmentStorageError
        now = datetime.now(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        day = stamp[:8]
        host = self._endpoint.netloc
        path = "/" + self._bucket + "/" + quote(key, safe="/")
        digest = hashlib.sha256(data).hexdigest()
        headers = {
            "host": host,
            "x-amz-content-sha256": digest,
            "x-amz-date": stamp,
            **(conditions or {}),
        }
        names = ";".join(sorted(headers))
        canonical_headers = "".join(
            f"{name}:{headers[name].strip()}\n" for name in sorted(headers)
        )
        canonical = f"{method}\n{path}\n{query}\n{canonical_headers}\n{names}\n{digest}"
        scope = f"{day}/{self._region}/s3/aws4_request"
        signing_key = ("AWS4" + self._credentials.secret_key).encode()
        for part in (day, self._region, "s3", "aws4_request"):
            signing_key = hmac.digest(signing_key, part.encode(), "sha256")
        to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                stamp,
                scope,
                hashlib.sha256(canonical.encode()).hexdigest(),
            )
        )
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._credentials.access_key}/{scope},"
            f"SignedHeaders={names},Signature={signature}"
        )
        client: http.client.HTTPConnection
        if self._endpoint.scheme == "https":
            client = http.client.HTTPSConnection(
                self._hostname,
                self._endpoint.port,
                timeout=5,
                context=ssl.create_default_context(),
            )
        else:
            client = http.client.HTTPConnection(
                self._hostname, self._endpoint.port, timeout=5
            )
        try:
            client.request(method, path + ("?" + query if query else ""), data, headers)
            response = client.getresponse()
            content = response.read(MAX_STORED_BYTES + 1)
            if len(content) > MAX_STORED_BYTES or (
                HTTPStatus.MULTIPLE_CHOICES <= response.status < HTTPStatus.BAD_REQUEST
            ):
                raise AttachmentStorageError
            return response.status, content, dict(response.getheaders())
        except (OSError, http.client.HTTPException) as error:
            raise AttachmentStorageError from error
        finally:
            client.close()

    def put(self, key: str, data: bytes) -> None:
        """Conditional create prevents overwrite, including on a worker replay."""
        status, _body, _headers = self._request(
            "PUT", self._key(key), data=data, conditions={"if-none-match": "*"}
        )
        if status == HTTPStatus.PRECONDITION_FAILED and self.get(key) == data:
            return
        if status != HTTPStatus.OK:
            raise AttachmentStorageError

    def get(self, key: str) -> bytes:
        """Bucket possession or a guessed key alone does not bypass service auth."""
        status, body, _headers = self._request("GET", self._key(key))
        if status != HTTPStatus.OK:
            raise AttachmentStorageError
        return body

    def replace(self, key: str, data: bytes) -> None:
        """Owner migration-only conditional replacement of an existing object."""
        key = self._key(key)
        status, _body, headers = self._request("HEAD", key)
        etag = next((v for k, v in headers.items() if k.lower() == "etag"), "")
        if status != HTTPStatus.OK or not etag:
            raise AttachmentStorageError
        status, _body, _headers = self._request(
            "PUT", key, data=data, conditions={"if-match": etag}
        )
        if status != HTTPStatus.OK:
            raise AttachmentStorageError

    def delete(self, key: str) -> None:
        """Remove a private object; absence is already success."""
        status, _body, _headers = self._request("DELETE", self._key(key))
        if status not in {HTTPStatus.OK, HTTPStatus.NO_CONTENT, HTTPStatus.NOT_FOUND}:
            raise AttachmentStorageError

    def mark_pending(self, key: str, organization_id: str) -> None:
        """Persist a content-free reconciliation reference before object PUT."""
        status, _body, _headers = self._request(
            "PUT", "pending/" + self._key(key), data=organization_id.encode("ascii")
        )
        if status != HTTPStatus.OK:
            raise AttachmentStorageError

    def clear_pending(self, key: str) -> None:
        """Drop the pending marker only after the owning database commit."""
        status, _body, _headers = self._request("DELETE", "pending/" + self._key(key))
        if status not in {HTTPStatus.OK, HTTPStatus.NO_CONTENT, HTTPStatus.NOT_FOUND}:
            raise AttachmentStorageError

    def pending_uploads(self) -> list[PendingUpload]:
        """Refuse an incomplete listing rather than lose orphan custody."""
        status, body, _headers = self._request(
            "GET", query="list-type=2&prefix=pending%2F"
        )
        if status != HTTPStatus.OK or b"<!DOCTYPE" in body.upper():
            raise AttachmentStorageError
        try:
            document = ET.fromstring(body)  # noqa: S314 - bounded, no DTD
        except ET.ParseError as error:
            raise AttachmentStorageError from error
        if document.findtext("{*}IsTruncated") == "true":
            raise AttachmentStorageError
        result: list[PendingUpload] = []
        for entry in document.findall("{*}Contents/{*}Key"):
            name = entry.text or ""
            if not name.startswith("pending/"):
                raise AttachmentStorageError
            key = self._key(name.removeprefix("pending/"))
            status, organization, _headers = self._request("GET", name)
            if status != HTTPStatus.OK or len(organization) != UUID_TEXT_LENGTH:
                raise AttachmentStorageError
            result.append(PendingUpload(key, organization.decode("ascii")))
        return result


def configured_storage(*, dev: bool = False) -> S3AttachmentStorage:
    """Construct the approved network adapter after the caller's lifecycle gate."""
    if dev and urlsplit(settings.SCRIBE_S3_ENDPOINT).hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise AttachmentStorageError
    store = secret_store()
    return S3AttachmentStorage(
        endpoint=settings.SCRIBE_S3_ENDPOINT,
        bucket=settings.SCRIBE_S3_BUCKET,
        region=settings.SCRIBE_S3_REGION,
        credentials=Credentials(
            store.get_secret("media-access-key"),
            store.get_secret("media-secret-key"),
        ),
        allow_loopback=dev,
    )
