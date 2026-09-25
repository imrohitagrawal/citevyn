"""Did this request come from our own site? (ADR-0005 §2, CSRF.)

Pure mechanism, no app imports: the caller supplies the allowlist.

``Origin`` and ``Sec-Fetch-Site`` are forbidden request headers — a page cannot
set or forge them — so when a browser sends them they are the truth about where
a request came from. The rule:

* An ``Origin`` other than ``null`` decides on its own: it must be on the
  allowlist (our site plus the CORS allowlist, which is the authority on which
  other origins may call us). ``Sec-Fetch-Site`` is not consulted, so a
  deliberately allowlisted cross-site frontend still works.
* ``Origin: null`` (a sandboxed frame, or Chromium under a ``no-referrer``
  policy) is trusted only when ``Sec-Fetch-Site`` says ``same-origin`` or
  ``none``. The same rule as the magic-link confirm route.
* No ``Origin``: browsers omit it on a same-origin GET, and non-browser clients
  never send it. Accepted unless ``Sec-Fetch-Site`` says the request is from
  another site, which a request hiding its origin should not be.

This is one of three CSRF layers, with the ``SameSite`` cookie and a fixed custom
header that a cross-site page cannot set without a CORS preflight.
"""

from __future__ import annotations

from collections.abc import Collection

_TRUSTED_FETCH_SITES = frozenset({"same-origin", "none"})


def _normalise(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def is_same_site_request(
    *, fetch_site: str | None, origin: str | None, allowed: Collection[str]
) -> bool:
    """True when the browser's own headers say the request came from us."""
    site = fetch_site.strip().lower() if fetch_site is not None else None
    if origin is not None:
        if _normalise(origin) == "null":
            return site in _TRUSTED_FETCH_SITES
        return _normalise(origin) in {_normalise(a) for a in allowed}
    return site is None or site in _TRUSTED_FETCH_SITES


__all__ = ["is_same_site_request"]
