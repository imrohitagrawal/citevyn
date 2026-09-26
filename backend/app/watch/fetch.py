"""Fetch one documentation page, politely and within fixed bounds.

Pure mechanism: no ``app.*`` imports (AGENTS.md). The rules, each tested:

* only ``https``, and only hosts on the allowlist (a redirect may not leave it);
* only the default port (443): a redirect to ``host:8443`` is refused;
* ``robots.txt`` is read once per host per run and obeyed (RFC 9309): a 4xx
  other than 429 means "no rules" (allowed); a 429, a 5xx, a network failure or
  an unparseable answer means the rules are unknown, so the host is skipped with
  a "robots.txt unreadable" error. Its own redirects are followed (up to
  :data:`MAX_ROBOTS_REDIRECTS`) on watched hosts only, and it is capped at
  :data:`MAX_ROBOTS_BYTES`;
* at most :data:`MAX_REDIRECTS` redirects for a page, each re-checked (scheme,
  host, port, robots.txt). Redirects are followed HERE, never by the HTTP
  client, whatever client the caller passes;
* uncompressed transfer is requested (``Accept-Encoding: identity``) and the
  body must be ``text/*`` and at most ``max_bytes``, streamed, so an oversized
  page is never held in memory;
* every request carries the watcher's own User-Agent;
* any malformed URL or redirect is a :class:`FetchError`, never a crash.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

MAX_REDIRECTS = 3
MAX_ROBOTS_REDIRECTS = 5
MAX_ROBOTS_BYTES = 512_000
_REDIRECTS = (301, 302, 303, 307, 308)
# httpx.InvalidURL is NOT an httpx.HTTPError; urljoin/urlsplit raise ValueError.
_BAD_URL = (httpx.InvalidURL, ValueError)


class FetchError(Exception):
    """A page could not be fetched; the message says why (no page text)."""


class RobotsCache:
    """robots.txt rules per host, read once per run through the same client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        user_agent: str,
        allowed_hosts: frozenset[str] | None = None,
    ) -> None:
        self._client = client
        self._ua = user_agent
        self._allowed = allowed_hosts
        self._rules: dict[str, RobotFileParser | None] = {}

    async def check(self, url: str) -> None:
        """Return if ``url`` may be fetched; else raise :class:`FetchError`."""
        host = urlsplit(url).netloc
        if host not in self._rules:
            self._rules[host] = await self._load(host)
        rules = self._rules[host]
        if rules is None:
            raise FetchError("robots.txt unreadable: the host is skipped this run")
        if not rules.can_fetch(self._ua, url):
            raise FetchError("disallowed by robots.txt")

    async def _load(self, host: str) -> RobotFileParser | None:
        """The host's rules, or None when they cannot be known."""
        url = f"https://{host}/robots.txt"
        allowed = self._allowed if self._allowed is not None else frozenset({host.split(":")[0]})
        for _ in range(MAX_ROBOTS_REDIRECTS + 1):
            try:
                _check(url, allowed)
                async with self._client.stream(
                    "GET",
                    url,
                    headers={"user-agent": self._ua, "accept-encoding": "identity"},
                    timeout=15.0,
                    follow_redirects=False,
                ) as res:
                    if res.status_code in _REDIRECTS and res.headers.get("location"):
                        url = urljoin(url, res.headers["location"])
                        continue
                    if res.status_code == 429 or res.status_code >= 500:
                        return None
                    if 400 <= res.status_code < 500:
                        parser = RobotFileParser()
                        parser.parse([])  # no robots.txt: everything allowed
                        return parser
                    if res.status_code != 200:
                        return None
                    body = bytearray()
                    async for chunk in res.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_ROBOTS_BYTES:
                            return None
            except (FetchError, httpx.HTTPError, *_BAD_URL):
                return None
            parser = RobotFileParser()
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            return parser
        return None


def _check(url: str, allowed_hosts: frozenset[str]) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchError("malformed URL") from exc
    if parts.scheme != "https":
        raise FetchError(f"not https: {parts.scheme}")
    if parts.hostname not in allowed_hosts:
        raise FetchError(f"host not on the watch list: {parts.hostname}")
    if port not in (None, 443):
        raise FetchError(f"port not allowed: {port}")


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    allowed_hosts: frozenset[str],
    user_agent: str,
    max_bytes: int,
    robots: RobotsCache,
) -> str:
    """The page's text, or :class:`FetchError`."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _check(current, allowed_hosts)
        await robots.check(current)
        try:
            async with client.stream(
                "GET",
                current,
                headers={"user-agent": user_agent, "accept-encoding": "identity"},
                timeout=20.0,
                follow_redirects=False,  # followed below, each hop re-checked
            ) as res:
                if res.status_code in _REDIRECTS:
                    location = res.headers.get("location")
                    if not location:
                        raise FetchError(f"redirect without a location ({res.status_code})")
                    current = urljoin(current, location)
                    continue
                if res.status_code != 200:
                    raise FetchError(f"HTTP {res.status_code}")
                ctype = res.headers.get("content-type", "").split(";")[0].strip().lower()
                if not ctype.startswith("text/"):
                    raise FetchError(f"unexpected content type: {ctype or 'none'}")
                body = bytearray()
                async for chunk in res.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise FetchError(f"page over the size cap ({max_bytes} bytes)")
                return body.decode(res.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise FetchError(f"network error: {type(exc).__name__}") from exc
        except _BAD_URL as exc:
            raise FetchError("malformed URL or redirect") from exc
    raise FetchError(f"too many redirects (more than {MAX_REDIRECTS})")


__all__ = [
    "MAX_REDIRECTS",
    "MAX_ROBOTS_BYTES",
    "MAX_ROBOTS_REDIRECTS",
    "FetchError",
    "RobotsCache",
    "fetch_page",
]
