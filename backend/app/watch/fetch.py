"""Fetch one documentation page, politely and within fixed bounds.

Pure mechanism: no ``app.*`` imports (AGENTS.md). The rules, each tested:

* only ``https``, and only hosts on the allowlist (a redirect may not leave it);
* ``robots.txt`` is read once per host per run and obeyed. A 4xx robots.txt
  means "no rules" (allowed); a 5xx or network failure means the rules are
  unknown, so the host is skipped;
* at most :data:`MAX_REDIRECTS` redirects;
* the body must be ``text/*`` and at most ``max_bytes`` (streamed, so an
  oversized page is never held in memory);
* every request carries the watcher's own User-Agent.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

MAX_REDIRECTS = 3


class FetchError(Exception):
    """A page could not be fetched; the message says why (no page text)."""


class RobotsCache:
    """robots.txt rules per host, read once per run through the same client."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str) -> None:
        self._client = client
        self._ua = user_agent
        self._rules: dict[str, RobotFileParser | None] = {}

    async def allows(self, url: str) -> bool:
        host = urlsplit(url).netloc
        if host not in self._rules:
            self._rules[host] = await self._load(host)
        rules = self._rules[host]
        return rules is not None and rules.can_fetch(self._ua, url)

    async def _load(self, host: str) -> RobotFileParser | None:
        parser = RobotFileParser()
        try:
            res = await self._client.get(
                f"https://{host}/robots.txt", headers={"user-agent": self._ua}, timeout=15.0
            )
        except httpx.HTTPError:
            return None  # unknown rules: skip the host
        if 400 <= res.status_code < 500:
            parser.parse([])  # no robots.txt: everything allowed
            return parser
        if res.status_code != 200:
            return None
        parser.parse(res.text.splitlines())
        return parser


def _check(url: str, allowed_hosts: frozenset[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchError(f"not https: {parts.scheme}")
    if parts.hostname not in allowed_hosts:
        raise FetchError(f"host not on the watch list: {parts.hostname}")


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
        if not await robots.allows(current):
            raise FetchError("disallowed by robots.txt")
        try:
            async with client.stream(
                "GET", current, headers={"user-agent": user_agent}, timeout=20.0
            ) as res:
                if res.status_code in (301, 302, 303, 307, 308):
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
    raise FetchError(f"too many redirects (more than {MAX_REDIRECTS})")


__all__ = ["MAX_REDIRECTS", "FetchError", "RobotsCache", "fetch_page"]
