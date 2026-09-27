"""Seed once; isolate scheduling worlds by verified PostgreSQL template clones."""

from __future__ import annotations

import copy
import hashlib
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import connection, connections
from django.db.backends.postgresql.base import DatabaseWrapper

from rbac_fixtures import rbac_graph as seed_graph

from .clock_probe_catalog import catalog_sha256
from .world_database import clone_database, content_digest, drop_database

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from rbac_fixtures import RbacGraph


class StaleWorldError(AssertionError):
    """A cloned world no longer matches the seed's content and catalog."""


@dataclass(frozen=True)
class Template:
    name: str
    graph: RbacGraph
    secret: Path
    content: str
    catalog: str
    secret_digest: str


@contextmanager
def connected(database: str) -> Iterator[None]:
    original = connections["default"]
    options = copy.deepcopy(original.settings_dict)
    options["NAME"] = database
    connections.close_all()
    configuration = connections.settings["default"]
    # New worker threads obtain their wrapper from ConnectionHandler.settings.
    # Updating only this thread's wrapper would send concurrency probes elsewhere.
    connections.settings["default"] = options
    connections["default"] = DatabaseWrapper(options, alias="default")
    try:
        yield
    finally:
        connections.close_all()
        connections.settings["default"] = configuration
        connections["default"] = original


class Worlds:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.template: Template | None = None
        self.live: set[str] = set()
        self.seed_count = 0
        self.clone_count = 0

    def seeded(self, secret_root: Path) -> Template:
        if self.template is None:
            source = str(connection.settings_dict["NAME"])
            assert source.startswith("test_")
            name = "scheduling_template_" + uuid4().hex
            connections.close_all()
            clone_database(name, source)
            self.live.add(name)
            try:
                with connected(name):
                    graph = seed_graph._get_wrapped_function()(secret_root)
                    catalog = catalog_sha256()
                secret = self.root / "tenant-kek.secret"
                shutil.copyfile(secret_root / "tenant-kek.secret", secret)
                secret.chmod(0o600)
                self.template = Template(
                    name,
                    graph,
                    secret,
                    content_digest(name),
                    catalog,
                    hashlib.sha256(secret.read_bytes()).hexdigest(),
                )
                self.seed_count += 1
            except BaseException:
                drop_database(name)
                self.live.remove(name)
                raise
        return self.template

    def check(self, database: str, held: Template) -> None:
        if (
            content_digest(database) != held.content
            or catalog_sha256() != held.catalog
            or hashlib.sha256(held.secret.read_bytes()).hexdigest()
            != held.secret_digest
        ):
            message = "scheduling template content or catalog changed after seeding"
            raise StaleWorldError(message)

    @contextmanager
    def world(self, secret_root: Path) -> Iterator[RbacGraph]:
        held = self.seeded(secret_root)
        name = "scheduling_world_" + uuid4().hex
        clone_database(name, held.name)
        self.live.add(name)
        try:
            with connected(name):
                self.check(name, held)
                shutil.copyfile(held.secret, secret_root / "tenant-kek.secret")
                self.clone_count += 1
                yield copy.deepcopy(held.graph)
        finally:
            drop_database(name)
            self.live.remove(name)

    def close(self) -> None:
        assert self.live == ({self.template.name} if self.template else set())
        if self.template is not None:
            drop_database(self.template.name)
            self.live.remove(self.template.name)
            self.template.secret.unlink()
