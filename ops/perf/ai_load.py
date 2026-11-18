"""Unbounded external producers calling real admission in the worker budget."""

from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from ops.perf.children import ChildContext, child_setup, wait_for_start
from ops.perf.rig import attach
from ops.perf.runtime import runtime_role

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from multiprocessing.synchronize import Event
    from types import TracebackType

START_TIMEOUT = 120
PRODUCERS = 2


def _worker(
    context: ChildContext,
    channel: Connection,
    ready: Event,
    failed: Event,
) -> None:
    """Resolve authority and spend real credit exclusively in the worker share."""
    try:
        attach("worker")
        child_setup(context)
        from apps.core.fairness import fair_acquire  # noqa: PLC0415

        organization_id = UUID(context.organization_id)
        with channel, runtime_role():
            ready.set()
            while True:
                try:
                    channel.recv_bytes(0)
                except EOFError:
                    break
                admitted = fair_acquire(
                    organization_id=organization_id, queue="ai-batch"
                )
                channel.send_bytes(b"1" if admitted else b"0")
    except BaseException:
        failed.set()
        ready.set()
        raise


def _producer(
    channel: Connection,
    stop: Event,
    warm: Event,
    counts: Any,  # noqa: ANN401 - multiprocessing's synchronized shared array
    failed: Event,
) -> None:
    """Issue synchronous requests without a rate, timer, sleep or backoff."""
    try:
        attach("producers")
        with channel:
            while not stop.is_set():
                channel.send_bytes(b"")
                admitted = channel.recv_bytes(1) == b"1"
                with counts.get_lock():
                    counts[0 if admitted else 1] += 1
                warm.set()
    except BaseException:
        failed.set()
        warm.set()
        raise


class AdmissionLoad:
    """Keep two producer loops and their real admission workers independent."""

    def __init__(self, neighbor: ChildContext, focus: ChildContext) -> None:
        """Create private channels; no tenant authority comes from a request."""
        self.spawn = multiprocessing.get_context("spawn")
        self.stop = self.spawn.Event()
        self.failed = self.spawn.Event()
        self.counts = self.spawn.Array("q", [0, 0])
        self.warm: list[Event] = []
        self.children: list[BaseProcess] = []
        self.started: list[BaseProcess] = []
        self.parent_channels: list[Connection] = []
        for _ in range(PRODUCERS):
            client = self._server(neighbor)
            warm = self.spawn.Event()
            self.warm.append(warm)
            self.children.append(
                self.spawn.Process(
                    target=_producer,
                    args=(client, self.stop, warm, self.counts, self.failed),
                )
            )
        self.victim = self._server(focus)
        self.parent_channels.remove(self.victim)

    def _server(self, context: ChildContext) -> Connection:
        client, server = self.spawn.Pipe()
        self.parent_channels.extend((client, server))
        ready = self.spawn.Event()
        self.warm.append(ready)
        self.children.append(
            self.spawn.Process(
                target=_worker, args=(context, server, ready, self.failed)
            )
        )
        return client

    def __enter__(self) -> Self:
        """Warm every real worker and producer before measuring clinical saves."""
        try:
            for child in self.children:
                child.start()
                self.started.append(child)
            for channel in self.parent_channels:
                channel.close()
            self.parent_channels.clear()
            wait_for_start(self.warm, self.failed, START_TIMEOUT)
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Finish in-flight requests, close channels and invalidate failed loads."""
        self.stop.set()
        self.victim.close()
        for channel in self.parent_channels:
            channel.close()
        self.parent_channels.clear()
        for child in self.started:
            child.join(START_TIMEOUT)
            if child.is_alive():
                self.failed.set()
                child.terminate()
                child.join()
            if child.exitcode != 0:
                self.failed.set()
        if self.failed.is_set() and error is None:
            message = "production admission load process failed"
            raise RuntimeError(message)

    def victim_admitted(self) -> bool:
        """Run the victim's real authority check in its own worker connection."""
        if self.failed.is_set():
            message = "production admission worker failed"
            raise RuntimeError(message)
        self.victim.send_bytes(b"")
        if not self.victim.poll(START_TIMEOUT):
            message = "production victim admission response timed out"
            raise RuntimeError(message)
        return self.victim.recv_bytes(1) == b"1"

    @property
    def first(self) -> int:
        """Return completed positive admission responses."""
        return int(self.counts[0])

    @property
    def second(self) -> int:
        """Return completed negative admission responses."""
        return int(self.counts[1])
