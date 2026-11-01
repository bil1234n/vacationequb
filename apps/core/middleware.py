"""Remembers the address the current request came in on (e.g. an ngrok URL or the PC's Wi-Fi IP).

Payment checkout / return / webhook links are built from it, so the app works whether the
backend is reached via localhost, a LAN IP, ngrok or a real domain - without editing .env.
"""
from contextvars import ContextVar

from django.conf import settings

_request_base: ContextVar[str | None] = ContextVar("rihla_request_base", default=None)


def public_base_url() -> str:
    """Base URL clients can reach: the current request's origin, else PUBLIC_BASE_URL."""
    return (_request_base.get() or settings.RIHLA["PUBLIC_BASE_URL"]).rstrip("/")


class RequestBaseURLMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            base = request.build_absolute_uri("/")
        except Exception:  # noqa: BLE001 - e.g. a disallowed host; fall back to settings
            base = None
        token = _request_base.set(base)
        try:
            return self.get_response(request)
        finally:
            _request_base.reset(token)
