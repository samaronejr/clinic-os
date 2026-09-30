"""Session middleware that never lets a stale request delete a live cookie."""

from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_vary_headers


class RotationSafeSessionMiddleware(SessionMiddleware):
    """Django's session middleware, minus cookie deletion by stale requests.

    Django deletes the session cookie on any response whose request carried a
    cookie that resolved to an empty session. OTP verification rotates the
    session key (``cycle_key``), and a request the browser sent before the
    rotation - the service worker's ``/sw.js`` update check, a status poll -
    arrives afterwards with the old key, reads an empty session and is
    refused. Its deleting ``Set-Cookie`` would then remove the browser's
    freshly rotated cookie and silently sign the user out.

    A request deletes the cookie only when it emptied the session itself
    (logout, flush on a hash mismatch). A request whose key resolved to no
    session and that changed nothing is answered without any cookie change.
    That grants nothing: ``cycle_key`` already deleted the old row, so the old
    key stays refused on every later request.
    """

    def process_response(
        self, request: HttpRequest, response: HttpResponse
    ) -> HttpResponse:
        """Leave the cookie alone for an unknown key; otherwise defer to Django."""
        try:
            stale = (
                settings.SESSION_COOKIE_NAME in request.COOKIES
                and not request.session.modified
                and request.session.is_empty()
            )
        except AttributeError:
            return response
        if stale:
            patch_vary_headers(response, ("Cookie",))
            return response
        return super().process_response(request, response)
