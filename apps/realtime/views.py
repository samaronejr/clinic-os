"""Separate WSGI ticket and ASGI stream endpoints at one same-origin path."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Final

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_POST
from ops.release.activation import LiveModeHaltedError
from redis.exceptions import RedisError

from apps.realtime.authorization import (
    TopicDeniedError,
    authorize_topics,
    authorize_topics_sync,
    validated_topics,
)
from apps.realtime.stream import EventStream
from apps.realtime.tickets import consume_ticket, issue_ticket

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase

MAX_BODY: Final = 2048
MAX_SESSION_KEY: Final = 64
logger = logging.getLogger(__name__)


def denial() -> JsonResponse:
    """Do not distinguish malformed, expired, reused, foreign or unknown topics."""
    return JsonResponse(
        {"code": "access_denied", "message_key": "subscription_unavailable"}, status=403
    )


def _session_key(request: HttpRequest) -> str:
    key = request.COOKIES.get(settings.SESSION_COOKIE_NAME, "")
    if not isinstance(key, str) or not key or len(key) > MAX_SESSION_KEY:
        raise TopicDeniedError
    return key


@require_POST
def ticket_view(request: HttpRequest) -> HttpResponseBase:
    """Mint a ticket only after CSRF, session, OTP and current topic permissions."""
    if not settings.REALTIME_ENABLED:
        return JsonResponse({"code": "unavailable"}, status=503)
    try:
        if len(request.body) > MAX_BODY:
            return denial()
        payload = json.loads(request.body)
        if not isinstance(payload, dict) or set(payload) != {"topics"}:
            return denial()
        topics = validated_topics(payload["topics"])
        session_key = _session_key(request)
        authorize_topics_sync(session_key=session_key, topics=topics)
        return JsonResponse(
            {"ticket": issue_ticket(session_key=session_key, topics=topics)}
        )
    except (TopicDeniedError, ValueError, UnicodeDecodeError):
        logger.info("realtime subscription denied")
        return denial()
    except RedisError:
        logger.warning("realtime ticket unavailable; polling required")
        return JsonResponse({"code": "unavailable"}, status=503)


async def stream_view(request: HttpRequest) -> HttpResponseBase:
    """Only this route is mounted by the realtime process, never application URLs."""
    if not settings.REALTIME_ENABLED:
        return JsonResponse({"code": "unavailable"}, status=503)
    try:
        if (
            request.method != "GET"
            or set(request.GET) != {"t"}
            or len(request.GET.getlist("t")) != 1
        ):
            return denial()
        session_key = _session_key(request)
        topics = await sync_to_async(consume_ticket)(
            ticket=request.GET["t"], session_key=session_key
        )
        subscription = await authorize_topics(session_key=session_key, topics=topics)
        stream = EventStream(subscription)
    except (TopicDeniedError, LiveModeHaltedError):
        return denial()
    except (RedisError, TimeoutError):
        logger.warning("realtime stream unavailable; polling required")
        return JsonResponse({"code": "unavailable"}, status=503)
    response = StreamingHttpResponse(stream.events(), content_type="text/event-stream")
    response["X-Accel-Buffering"] = "no"
    return response
