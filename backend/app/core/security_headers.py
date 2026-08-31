"""Security response headers for the FastAPI app (ADR-0004 PR 2).

Installed unconditionally on every response — including the StaticFiles
mount at ``/`` (``app/main.py:_mount_frontend``), which is a *different*
response path from the router-generated JSON responses and would silently
miss these headers if the middleware only wrapped ``app.routes``.
:class:`BaseHTTPMiddleware` wraps the whole ASGI stack, mount included, so
one middleware covers both.

Why now, ahead of any login code: once a session cookie exists (ADR-0004
PR 3), these headers are what stops that cookie from being read by an
injected script (XSS) or the app being framed by a hostile page
(clickjacking) to trick a logged-in visitor into an unintended action.
Shipping them before the cookie exists means there is no window where a
cookie is live without the headers that protect it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.config import Settings

# No 'unsafe-inline'/'unsafe-eval' on script-src: the built frontend bundle
# (Vite) ships no inline script — verified by inspecting `frontend/dist/
# index.html` after a real `npm run build`, not assumed. connect-src 'self'
# is enough for the SPA's same-origin fetches (app/main.py serves both the UI
# and the API from one origin by design, see infra/docker/Dockerfile.api) —
# no third-party API host is called from script.
#
# style-src and font-src DO need third-party origins: `frontend/index.html`
# pulls Google Fonts and Fontshare stylesheets (each `<link rel="stylesheet"
# href="https://...">` is itself a style-src load) and the font files those
# stylesheets reference come from fonts.gstatic.com / api.fontshare.com. A
# same-origin-only CSP here would silently break the loaded theme — the
# error is invisible unless you check the browser console, so this is listed
# explicitly rather than tightened blindly. `backend/tests/
# test_security_headers.py` pins these exact hosts so an unrelated frontend
# change (a new font/style CDN) can't widen the policy unnoticed.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com https://api.fontshare.com; "
    "font-src 'self' https://fonts.gstatic.com https://api.fontshare.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def apply_security_headers(response: Response, *, hsts: bool) -> Response:
    """Stamp the security headers directly onto ``response`` and return it.

    Shared by :class:`SecurityHeadersMiddleware` and, critically,
    ``app.main._unhandled_exception_handler``. Starlette wires a bare
    ``Exception`` handler into ``ServerErrorMiddleware`` — the framework's
    OUTERMOST wrapper, added around every user middleware including this
    one (``Starlette.build_middleware_stack``: ``[ServerErrorMiddleware] +
    user_middleware + [ExceptionMiddleware]``). When that handler's response
    is sent, it never passes back through ``SecurityHeadersMiddleware``, so
    an unhandled non-HTTP exception (anything not registered with
    ``ExceptionMiddleware`` — i.e. not ``StarletteHTTPException`` /
    ``OrchestratorError`` / ``RequestValidationError``) would otherwise ship
    with none of these headers. Verified empirically: a route raising a bare
    ``ValueError`` returned a 500 with zero security headers before this
    function existed (caught by a security review of this PR, reproduced,
    fixed here rather than by restructuring the middleware — no ASGI
    middleware registered via ``add_middleware`` can wrap outside
    ``ServerErrorMiddleware``, so the header-carrying middleware alone can
    never cover this path).
    """
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _CSP
    if hsts:
        # 1 year, subdomains included, no preload submission (that is a
        # one-way door — see docs/ADR/0004-user-accounts.md's non-goals).
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        super().__init__(app)
        # HSTS only over a connection that is actually HTTPS-terminated.
        # Fly's edge proxy terminates TLS in production (docs/DEPLOY_FLY.md
        # §0); local `uvicorn --reload` and hermetic tests serve plain HTTP,
        # where an HSTS header is a lie the browser would otherwise act on —
        # it would upgrade a legitimate local http:// visit and fail closed.
        self._hsts = hsts

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        return apply_security_headers(response, hsts=self._hsts)


def configure_security_headers(app: FastAPI, settings: Settings) -> None:
    """Install :class:`SecurityHeadersMiddleware` on ``app``.

    A function (not a raw ``add_middleware`` call in ``main.py``) so tests
    can build an app against an arbitrary :class:`Settings`, matching the
    ``configure_cors`` convention this module mirrors.
    """
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.environment == "production")


__all__ = ["SecurityHeadersMiddleware", "apply_security_headers", "configure_security_headers"]
