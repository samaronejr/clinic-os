"""Clinical Inbox read model: one typed queue assembled from registered feeds.

Each owning domain registers a feed with ``register_inbox_feed(kind, query)``;
the read model never stores a copy, so it is rebuilt on every query from the
domain's own committed, RLS-bounded records. A feed decides its own record
authority; this module decides only who may open the inbox at all and how the
items are ordered, filtered and counted.

The inbox opens for clinic ``demographics.read`` held through a clinical role
whose bundle carries a result or order observation permission (``result.read``,
``order.observe``). Those permissions are patient-scoped, so they cannot be
decided for a whole clinic; each feed rechecks them per patient. The gate
refuses like every workflow boundary; the typed API turns that refusal, and an
unknown or foreign clinic, into the same empty page a clinician with nothing to
do receives, so no answer reveals whether a clinic or record exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
    require_permission,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.identity.permissions import BUNDLES_V2
from apps.realtime.transport import publish_on_commit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from uuid import UUID

    type InboxQuery = Callable[[InboxScope], Iterable[InboxItem]]

INBOX_PERMISSIONS: Final = frozenset({"result.read", "order.observe"})
INBOX_ROLES: Final = tuple(
    UserClinicRole.Role(role)
    for role, bundle in sorted(BUNDLES_V2.items())
    if bundle & INBOX_PERMISSIONS
)
MAX_ITEMS: Final = 200
MAX_KNOWN_KEYS: Final = 400
KIND_PATTERN: Final = re.compile(r"[a-z][a-z_]{0,23}")
KEY_PATTERN: Final = re.compile(
    r"([a-z][a-z_]{0,23}):"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
OWNER_FILTERS: Final = ("all", "mine")
DUE_FILTERS: Final = ("all", "overdue", "today")
PRIORITY_RANK: Final = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


class InboxInputError(ValueError):
    """A filter or known-key list outside the closed query vocabulary."""

    def __init__(self) -> None:
        """Never reflect the rejected input."""
        super().__init__("invalid inbox query")


@dataclass(frozen=True, slots=True)
class InboxScope:
    """What a feed may rely on: the clinic, the GUC actor and the clinic clock.

    ``limit`` is the most items a feed returns, most urgent first; it is one
    more than a page holds so the read model can tell when a feed overflowed.
    """

    clinic_id: UUID
    actor_id: UUID
    timezone: str
    now: datetime
    limit: int = MAX_ITEMS + 1


@dataclass(frozen=True, slots=True)
class InboxObservation:
    """One exact source observation shown in a per-item preview."""

    code: str
    label: str
    value: str
    unit: str
    reference_interval: str
    flag: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InboxItem:
    """One actionable row; ``key`` is the stable identity a refresh compares."""

    kind: str
    record_id: UUID
    revision: int
    title: str
    target_id: UUID | None = None
    patient_label: str = ""
    owner: str = "me"
    owner_label: str = ""
    due_at: datetime | None = None
    priority: str = "normal"
    critical: bool = False
    action: str = ""
    bulk_eligible: bool = False
    preview: tuple[InboxObservation, ...] = ()
    overdue: bool = False
    key: str = field(init=False)

    def __post_init__(self) -> None:
        """Derive the opaque item identity used for removal notices."""
        object.__setattr__(self, "key", f"{self.kind}:{self.record_id}")


@dataclass(frozen=True, slots=True)
class InboxFilters:
    """The closed filter vocabulary of one inbox query."""

    kinds: tuple[str, ...] = ()
    owner: str = "all"
    due: str = "all"


@dataclass(frozen=True, slots=True)
class InboxPage:
    """One authorized, triage-ordered page plus counts and removal notices.

    ``counts`` covers every visible item before filtering so Today and the
    navigation can report work outside the current filter; ``removed`` lists
    the caller-supplied keys that are no longer visible to this actor. A kind
    in ``overflowed`` had more items than a page holds: only its most urgent
    items are kept, its count is a lower bound and ``truncated`` is set.
    """

    items: tuple[InboxItem, ...]
    counts: dict[str, int]
    total: int
    overdue: int
    critical: int
    removed: tuple[str, ...]
    truncated: bool
    generated_at: datetime
    overflowed: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _Feed:
    query: InboxQuery
    label: str
    position: int


_FEEDS: dict[str, _Feed] = {}


def register_inbox_feed(
    kind: str, query: InboxQuery, *, label: str, position: int
) -> None:
    """Register one domain feed; re-registering the same query is idempotent.

    ``label`` is the untranslated msgid naming the feed's work; ``position``
    fixes the display order independent of application loading order. The
    query returns at most ``scope.limit`` authorized items, most urgent first.
    """
    if KIND_PATTERN.fullmatch(kind) is None:
        msg = "inbox feed kind must be a short lowercase identifier"
        raise ValueError(msg)
    registered = _FEEDS.get(kind)
    if registered is not None and registered.query is not query:
        msg = f"inbox feed {kind} is already registered"
        raise ValueError(msg)
    _FEEDS[kind] = _Feed(query, label, position)
    ordered = sorted(_FEEDS.items(), key=lambda entry: (entry[1].position, entry[0]))
    _FEEDS.clear()
    _FEEDS.update(ordered)


def registered_kinds() -> tuple[str, ...]:
    """Return the registered feed kinds in their stable display order."""
    return tuple(_FEEDS)


def kind_label(kind: str) -> str:
    """Translate a registered feed's label at the display boundary."""
    return _(_FEEDS[kind].label)


def validated_filters(
    *, kinds: Iterable[str] = (), owner: str = "all", due: str = "all"
) -> InboxFilters:
    """Bound every filter to the registered and closed vocabularies."""
    selected = tuple(dict.fromkeys(kinds))
    if (
        any(kind not in _FEEDS for kind in selected)
        or owner not in OWNER_FILTERS
        or due not in DUE_FILTERS
    ):
        raise InboxInputError
    return InboxFilters(kinds=selected, owner=owner, due=due)


def validated_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """Accept only well-formed keys of registered kinds, bounded in number."""
    values = tuple(dict.fromkeys(keys))
    if len(values) > MAX_KNOWN_KEYS:
        raise InboxInputError
    for value in values:
        match = KEY_PATTERN.fullmatch(value)
        if match is None or match[1] not in _FEEDS:
            raise InboxInputError
    return values


def require_inbox(*, clinic_id: UUID) -> UUID:
    """Require clinic demographics authority held through an inbox role."""
    actor = require_permission("demographics.read", clinic_id=clinic_id)
    require_current_actor_clinic_roles(clinic_id, INBOX_ROLES)
    return actor


def _sort_key(item: InboxItem) -> tuple[object, ...]:
    return (
        not item.critical,
        PRIORITY_RANK.get(item.priority, len(PRIORITY_RANK)),
        not item.overdue,
        item.due_at is None,
        item.due_at or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
        item.kind,
        str(item.record_id),
    )


def _matches(
    item: InboxItem, filters: InboxFilters, zone: ZoneInfo, now: datetime
) -> bool:
    if filters.kinds and item.kind not in filters.kinds:
        return False
    if filters.owner == "mine" and item.owner != "me":
        return False
    if filters.due == "overdue":
        return item.overdue
    if filters.due == "today":
        return item.due_at is not None and (
            item.due_at.astimezone(zone).date() <= now.astimezone(zone).date()
        )
    return True


def empty_inbox(known: tuple[str, ...] = ()) -> InboxPage:
    """Return the page every unauthorized or clinic-less query receives."""
    return InboxPage(
        items=(),
        counts=dict.fromkeys(_FEEDS, 0),
        total=0,
        overdue=0,
        critical=0,
        removed=known,
        truncated=False,
        generated_at=timezone.now(),
    )


def query_inbox(
    *,
    clinic_id: UUID,
    filters: InboxFilters | None = None,
    known: tuple[str, ...] = (),
) -> InboxPage:
    """Assemble the actor's inbox from every feed the actor may read.

    The inbox gate refuses like any other permission boundary. A feed whose own
    permission the actor lacks (for example a narrowed ``tasks.view``) is left
    out instead of failing the whole queue.
    """
    actor = require_inbox(clinic_id=clinic_id)
    selected = filters or InboxFilters()
    now = timezone.now()
    clinic = Clinic.objects.get(pk=clinic_id)
    scope = InboxScope(
        clinic_id=clinic_id,
        actor_id=actor,
        timezone=str(clinic.timezone),
        now=now,
        limit=MAX_ITEMS + 1,
    )
    visible: list[InboxItem] = []
    overflowed: set[str] = set()
    for kind, feed in _FEEDS.items():
        try:
            produced = tuple(feed.query(scope))
        except CurrentActorError:
            continue
        if len(produced) > MAX_ITEMS:
            overflowed.add(kind)
            produced = produced[:MAX_ITEMS]
        for item in produced:
            if item.kind != kind:
                msg = f"inbox feed {kind} produced another kind"
                raise ValueError(msg)
            visible.append(
                replace(item, overdue=item.due_at is not None and item.due_at < now)
            )
    visible.sort(key=_sort_key)
    keys = {item.key for item in visible}
    zone = ZoneInfo(str(clinic.timezone))
    matching = [item for item in visible if _matches(item, selected, zone, now)]
    counts = dict.fromkeys(_FEEDS, 0)
    for item in visible:
        counts[item.kind] += 1
    return InboxPage(
        items=tuple(matching[:MAX_ITEMS]),
        counts=counts,
        total=len(visible),
        overdue=sum(item.overdue for item in visible),
        critical=sum(item.critical for item in visible),
        removed=tuple(key for key in known if key not in keys),
        truncated=bool(overflowed) or bool(matching[MAX_ITEMS:]),
        generated_at=now,
        overflowed=frozenset(overflowed),
    )


def publish_inbox_hint(clinic_id: UUID) -> None:
    """Ask subscribed inboxes to refetch after commit; the hint carries no record."""
    if settings.REALTIME_ENABLED:
        publish_on_commit(topic=f"clinic:{clinic_id}:inbox", kind="inbox", version=1)


def owner_label(owner: str) -> str:
    """Name the closed owner vocabulary at the display boundary."""
    return {
        "me": _("You"),
        "role": _("Your role"),
        "team": _("Care team"),
        "unassigned": _("Unassigned"),
    }[owner]
