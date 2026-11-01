"""Reviewed front-desk knowledge base (todo 54, ADR-007/D-15).

A clinic administrator (``configuration.clinic``) drafts administrative
answers (hours, prices, services, preparation, policies) and approves each exact
version; approving a version retires the key's previous approved version. The
agent answers only from approved versions, by deterministic keyword lookup: the
article whose keywords match the most words of the folded message wins, a tie or
no match answers nothing (the agent then hands off). No vector store, no model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.db import transaction
from django.utils import timezone

from apps.ai.front_desk_models import (
    MAX_BODY_CHARS,
    MAX_KEYWORDS,
    TOPICS,
    KnowledgeArticle,
)
from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

__all__ = (
    "ArticleAnswer",
    "FrontDeskAccessDeniedError",
    "FrontDeskConflictError",
    "FrontDeskInputError",
    "approve_article",
    "best_answer",
    "draft_article",
    "fold",
    "knowledge_articles",
    "retire_article",
)

KEY: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
WORD: Final = re.compile(r"[a-z0-9]+")
# Administrative answers name no person, address, phone or link: those belong
# in approved templates, never in free answer text.
_IDENTIFYING: Final = re.compile(r"@|https?://|www\.|\d{4,}")
MAX_TITLE_CHARS: Final = 120
MAX_LINK_CHARS: Final = 200
# Only a plain https link without query or fragment: nothing can carry an
# identifier or token to the patient.
LINK: Final = re.compile(r"https://[a-z0-9.-]+(?:/[A-Za-z0-9._~/-]*)?")


class FrontDeskAccessDeniedError(CurrentActorError):
    """Unknown and foreign records share one payload-free denial."""

    def __init__(self) -> None:
        """Do not reflect identifiers or distinguish unknown from foreign."""
        super().__init__("current actor unauthorized")


class FrontDeskConflictError(RuntimeError):
    """The record changed since it was read; review it again."""

    def __init__(self) -> None:
        """Carry no record detail."""
        super().__init__("front desk conflict")


class FrontDeskInputError(ValueError):
    """Reject terms outside the closed article contract."""

    def __init__(self) -> None:
        """Carry no submitted content."""
        super().__init__("front desk input refused")


@dataclass(frozen=True, slots=True)
class ArticleAnswer:
    """One approved version the agent may cite: key and version, never a draft."""

    article_id: UUID
    key: str
    version: int
    title: str
    body: str
    keywords: tuple[str, ...]
    link: str = ""


def fold(text: str) -> str:
    """Fold case and accents so 'Horário' and 'horario' are one word."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _words(text: str) -> frozenset[str]:
    return frozenset(WORD.findall(fold(text)))


def best_answer(articles: Iterable[ArticleAnswer], text: str) -> ArticleAnswer | None:
    """Return the single best-matching approved article, or None.

    The score is the number of the article's keywords present as words of the
    message; zero or a tie between the best two answers nothing.
    """
    words = _words(text)
    scored = sorted(
        (
            (len(words & set(article.keywords)), article.key, article)
            for article in articles
        ),
        key=lambda entry: (-entry[0], entry[1]),
    )
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    return scored[0][2]


def _terms(  # noqa: PLR0913 - the closed article terms
    key: str,
    topic: str,
    title: str,
    body: str,
    keywords: tuple[str, ...],
    link: str,
) -> tuple[str, str, tuple[str, ...]]:
    clean_title = " ".join(title.split()) if type(title) is str else ""
    clean_body = " ".join(body.split()) if type(body) is str else ""
    folded = tuple(
        sorted({fold(word).strip() for word in keywords if type(word) is str})
    )
    if (
        type(key) is not str
        or KEY.fullmatch(key) is None
        or topic not in TOPICS
        or not 0 < len(clean_title) <= MAX_TITLE_CHARS
        or not 0 < len(clean_body) <= MAX_BODY_CHARS
        or _IDENTIFYING.search(clean_body)
        or not 0 < len(folded) <= MAX_KEYWORDS
        or any(WORD.fullmatch(word) is None for word in folded)
        or type(link) is not str
        or (link != "" and (LINK.fullmatch(link) is None or len(link) > MAX_LINK_CHARS))
    ):
        raise FrontDeskInputError
    return clean_title, clean_body, folded


@transaction.atomic
def draft_article(  # noqa: PLR0913 - the closed article terms
    *,
    clinic_id: UUID,
    key: str,
    topic: str,
    title: str,
    body: str,
    keywords: tuple[str, ...],
    link: str = "",
) -> KnowledgeArticle:
    """Draft the next version of one answer; nothing is answered until approval."""
    actor = require_permission("configuration.clinic", clinic_id=clinic_id)
    clean_title, clean_body, folded = _terms(key, topic, title, body, keywords, link)
    clinic = Clinic.objects.get(pk=clinic_id)
    latest = (
        KnowledgeArticle.objects.select_for_update()
        .filter(clinic_id=clinic_id, key=key)
        .order_by("-version")
        .first()
    )
    article = KnowledgeArticle.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        key=key,
        version=latest.version + 1 if latest else 1,
        topic=topic,
        title=clean_title,
        body=clean_body,
        keywords=list(folded),
        link=link,
        created_by_id=actor,
    )
    record_phase1_event(
        "ai.knowledge_article.drafted",
        clinic_id=clinic_id,
        affected_record_id=article.pk,
    )
    return article


def _article(clinic_id: UUID, article_id: UUID) -> KnowledgeArticle:
    article = (
        KnowledgeArticle.objects.select_for_update()
        .filter(pk=article_id, clinic_id=clinic_id)
        .first()
    )
    if article is None:
        raise FrontDeskAccessDeniedError
    return article


@transaction.atomic
def approve_article(
    *, clinic_id: UUID, article_id: UUID, expected_revision: int
) -> KnowledgeArticle:
    """Approve this exact draft version; the key's previous version retires."""
    actor = require_permission("configuration.clinic", clinic_id=clinic_id)
    article = _article(clinic_id, article_id)
    if article.revision != expected_revision or article.state != "draft":
        raise FrontDeskConflictError
    now = timezone.now()
    for previous in KnowledgeArticle.objects.select_for_update().filter(
        clinic_id=clinic_id, key=article.key, state="approved"
    ):
        previous.state = "retired"
        previous.retired_at = now
        previous.revision += 1
        previous.save(update_fields=("state", "retired_at", "revision"))
    article.state = "approved"
    article.approved_by_id = actor
    article.approved_at = now
    article.revision += 1
    article.save(update_fields=("state", "approved_by", "approved_at", "revision"))
    record_phase1_event(
        "ai.knowledge_article.approved",
        clinic_id=clinic_id,
        affected_record_id=article.pk,
    )
    return article


@transaction.atomic
def retire_article(
    *, clinic_id: UUID, article_id: UUID, expected_revision: int
) -> KnowledgeArticle:
    """Stop answering from an approved version; history is kept."""
    require_permission("configuration.clinic", clinic_id=clinic_id)
    article = _article(clinic_id, article_id)
    if article.revision != expected_revision or article.state != "approved":
        raise FrontDeskConflictError
    article.state = "retired"
    article.retired_at = timezone.now()
    article.revision += 1
    article.save(update_fields=("state", "retired_at", "revision"))
    record_phase1_event(
        "ai.knowledge_article.retired",
        clinic_id=clinic_id,
        affected_record_id=article.pk,
    )
    return article


def knowledge_articles(*, clinic_id: UUID) -> tuple[KnowledgeArticle, ...]:
    """List every version of the clinic's answers for its administrators."""
    require_permission("configuration.clinic", clinic_id=clinic_id)
    return tuple(
        KnowledgeArticle.objects.filter(clinic_id=clinic_id).order_by(
            "key", "-version"
        )[:200]
    )
