"""Approved managed-secret boundary for key material.

The task-6 managed-secrets record requires one narrow contract: secrets are
read through a ``SecretStore`` selected by explicit configuration, never
through ad-hoc environment reads or defaults. There is no plaintext fallback:
when no backend is configured, or the configured backend cannot return
material, access fails closed with ``SecretUnavailableError``.

``synthetic-file`` is the only implemented backend. It reads one hex-encoded
secret per file from a root owned by the deploying operator and is the
rehearsal stand-in for the approved managed store; it is never a default.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Protocol

from django.conf import settings

_SECRET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class SecretUnavailableError(Exception):
    """No configured backend, or the backend cannot return the secret."""


class SecretStoreContractError(Exception):
    """The configured backend returned material outside the contract."""


class SecretStore(Protocol):
    """One named-secret lookup; implementations never log or echo material."""

    def get_secret(self, name: str) -> str:
        """Return the secret value or raise SecretUnavailableError."""
        ...


class FileSecretStore:
    """Synthetic file backend: one ``<name>.secret`` file per secret.

    The file must be a regular file with owner-only permissions (0600 or
    stricter) and contain exactly one 64-hex-character value. Anything else
    fails closed so a misconfigured store can never yield weak material.
    """

    def __init__(self, root: str) -> None:
        """Bind the backend to one operator-provisioned root directory."""
        if type(root) is not str or not root:
            raise SecretUnavailableError
        self._root = Path(root)

    def get_secret(self, name: str) -> str:
        """Return the file's hex secret after strict shape checks."""
        if type(name) is not str or not _SECRET_NAME.fullmatch(name):
            raise SecretUnavailableError
        path = self._root / f"{name}.secret"
        try:
            info = os.lstat(path)
        except OSError as error:
            raise SecretUnavailableError from error
        if not stat.S_ISREG(info.st_mode):
            raise SecretUnavailableError
        if info.st_mode & 0o777 not in (0o600, 0o400):
            raise SecretUnavailableError
        try:
            with path.open(encoding="ascii") as handle:
                value = handle.read().strip()
        except (OSError, UnicodeDecodeError) as error:
            raise SecretUnavailableError from error
        if not _HEX_64.fullmatch(value):
            raise SecretUnavailableError
        return value


def secret_store() -> SecretStore:
    """Resolve the explicitly configured backend or fail closed.

    ``CLINIC_SECRET_BACKEND`` selects the backend; ``CLINIC_SECRET_DIR``
    locates the synthetic-file root. An unknown name or missing directory is
    a configuration failure, not a fallback to another source.
    """
    backend = getattr(settings, "CLINIC_SECRET_BACKEND", None)
    if backend is None:
        backend = os.environ.get("CLINIC_SECRET_BACKEND")
    if backend == "synthetic-file":
        root = getattr(settings, "CLINIC_SECRET_DIR", None)
        if root is None:
            root = os.environ.get("CLINIC_SECRET_DIR")
        if type(root) is not str or not root:
            raise SecretUnavailableError
        return FileSecretStore(root)
    raise SecretUnavailableError
