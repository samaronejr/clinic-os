"""Immutable PostgreSQL image, volume-role, and HBA contracts for HTTPS QA."""

from __future__ import annotations

from typing import Final

POSTGRES_IMAGE: Final = (
    "docker.io/library/postgres@"
    "sha256:4c9405bdf36a7a96c5637acec4b39545681f0d2154a7b1e622890607aad6bf56"
)
VOLUME_SUFFIXES: Final = (
    "phase1a-pki-private",
    "phase1a-source-db-tls",
    "phase1a-restore-db-tls",
    "phase1a-web-tls",
    "phase1a-db-trust",
    "phase1a-materializer-pgdata",
)
HBA_RULES: Final = (
    "local all postgres peer",
    "local all all reject",
    "hostnossl all all 0.0.0.0/0 reject",
    "hostnossl all all ::/0 reject",
    "hostssl all all 0.0.0.0/0 scram-sha-256",
    "hostssl all all ::/0 scram-sha-256",
)
SOURCE_DATABASE_HOST: Final = "phase1a-db.qa.clinic-os.dev"
RESTORE_DATABASE_HOST: Final = "phase1a-restore-db.qa.clinic-os.dev"
WEB_HOST: Final = "phase1a.qa.clinic-os.dev"
