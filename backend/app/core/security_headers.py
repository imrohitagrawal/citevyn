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
# style-src and font-src are now 'self' ONLY, and that is a consequence of #365
# rather than a tightening done for its own sake. Both pages self-host their two
# faces: `frontend/src/styles/fonts.css` and `frontend/public/about.css` declare
# the `@font-face` rules and `frontend/public/fonts/*.woff2` holds the bytes, so
# nothing in either page's critical path is off-origin any more. Nothing is left
# to permit, so nothing is permitted.
#
# WHY THE THIRD-PARTY ENTRIES WERE THERE, kept because the trap they encode is
# the reason this stays 'self'-only rather than drifting back. A `<link
# rel="stylesheet" href="https://...">` is itself a style-src load, and the font
# FILES that stylesheet references come from a DIFFERENT host:
#
#     stylesheet                  font files
#     fonts.googleapis.com   ->   fonts.gstatic.com
#     api.fontshare.com      ->   cdn.fontshare.com
#
# That split is the whole trap (#306). `cdn.fontshare.com` was missing here, so
# the browser logged 12 CSP violations on every page load — one per `src` URL
# across 4 weights x 3 formats — and nothing looked broken. The Google pair
# beside it was correct, which is exactly why the identical split was missed.
# (Measured: no pixel changed either way, because no `font-family` used Satoshi.
# The errors were real; the "falls back to a system font" in #306 was not. #316.)
#
# So: putting ANY font or style CDN back needs BOTH its hosts here, and it puts
# a third party back in front of first paint — which is the #365 defect, where a
# render-blocking third-party stylesheet also blocks `<script type="module">`
# from executing and leaves the visitor on a blank page. Prefer self-hosting.
#
# Both directions are pinned. `test_security_headers.py` pins this constant and
# the emitted header byte-exact, so a new host cannot widen the policy
# unnoticed. `test_csp_covers_the_pages_real_origins.py` pins the OTHER
# direction — it parses `frontend/index.html` AND the rendered `/about` page and
# fails if either loads an origin this policy does not permit — so re-adding a
# font CDN to one page and forgetting the other is caught, in either order.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "font-src 'self'; "
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
