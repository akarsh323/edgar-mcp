"""HTTP access to SEC EDGAR, live or from a recorded snapshot.

The client is the only module that touches the network. It enforces the
SEC's fair-access rules (a declared User-Agent and a request-rate ceiling),
retries transient failures with backoff, honours `Retry-After`, and caches
responses for the life of the process.

Offline mode swaps the HTTP transport for `SnapshotTransport`, which serves
files laid out like the SEC's own URLs. Everything above the transport runs
the same code in both modes.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import gzip
import json
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from edgar_mcp.config import ENV_USER_AGENT, Settings

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
TICKERS_URL = f"{SEC_WWW}/files/company_tickers.json"

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 20.0
CACHE_TTL_SECONDS = 600.0
CACHE_ENTRIES = 256

Sleep = Callable[[float], Awaitable[None]]


def submissions_url(cik: int) -> str:
    """URL of a company's filing index."""
    return f"{SEC_DATA}/submissions/CIK{cik:010d}.json"


def concept_url(cik: int, taxonomy: str, tag: str) -> str:
    """URL of one XBRL concept's reported values for a company."""
    base = f"{SEC_DATA}/api/xbrl/companyconcept/CIK{cik:010d}"
    return f"{base}/{taxonomy}/{tag}.json"


def archive_url(cik: int, accession_number: str, document: str) -> str:
    """URL of a document inside a filing."""
    folder = accession_number.replace("-", "")
    return f"{SEC_WWW}/Archives/edgar/data/{cik}/{folder}/{document}"


class EdgarError(Exception):
    """Base class for EDGAR access failures."""


class EdgarNotFoundError(EdgarError):
    """The SEC has no resource at this URL."""


class EdgarUnavailableError(EdgarError):
    """The SEC did not answer successfully after retries."""


class EdgarConfigError(EdgarError):
    """The client is not configured to make this request."""


class RateLimiter:
    """Spaces request starts at least `1 / rate` seconds apart."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Creates a limiter.

        Args:
          rate: Maximum requests per second.
          clock: Monotonic clock, injectable for tests.
          sleep: Async sleep, injectable for tests.
        """
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Waits until the next request may start."""
        async with self._lock:
            now = self._clock()
            if now < self._next:
                await self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


class SnapshotTransport(httpx.AsyncBaseTransport):
    """Serves recorded responses from a directory laid out like the URLs.

    `https://data.sec.gov/submissions/CIK0000320193.json` is served from
    `<root>/data.sec.gov/submissions/CIK0000320193.json`, or from the same
    path with a `.gz` suffix. A missing file is a 404, as it would be live.
    """

    def __init__(self, root: Path) -> None:
        """Creates a transport over `root`."""
        self._root = root

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        """Returns the recorded response for `request`."""
        path = self._root / request.url.host / request.url.path.lstrip("/")
        compressed = path.with_name(path.name + ".gz")
        if compressed.is_file():
            body = gzip.decompress(compressed.read_bytes())
        elif path.is_file():
            body = path.read_bytes()
        else:
            return httpx.Response(404, text="Not Found", request=request)
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
            request=request,
        )


class EdgarClient:
    """Fetches EDGAR resources with rate limiting, retries and caching."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Creates a client.

        Args:
          settings: Where to read from and how to identify ourselves.
          transport: HTTP transport override, for tests. Defaults to the
            snapshot in offline mode and to the network otherwise.
          sleep: Async sleep used for backoff and rate limiting.
          clock: Monotonic clock used for the cache and rate limiting.
        """
        self.settings = settings
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._limiter = RateLimiter(
            settings.max_requests_per_second, clock=clock, sleep=sleep
        )
        self._cache: collections.OrderedDict[str, tuple[float, str]] = (
            collections.OrderedDict()
        )
        self._http: httpx.AsyncClient | None = None
        self.requests_sent = 0

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            transport = self._transport
            if transport is None and self.settings.snapshot_dir is not None:
                transport = SnapshotTransport(self.settings.snapshot_dir)
            headers = {"Accept-Encoding": "gzip, deflate"}
            if self.settings.user_agent:
                headers["User-Agent"] = self.settings.user_agent
            self._http = httpx.AsyncClient(
                transport=transport,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        return self._http

    async def aclose(self) -> None:
        """Closes the underlying HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get_text(self, url: str) -> str:
        """Returns the body of `url` as text.

        Raises:
          EdgarNotFoundError: On HTTP 404.
          EdgarConfigError: If no User-Agent is configured for live mode,
            or the SEC rejects the request as undeclared (HTTP 403).
          EdgarUnavailableError: If the request still fails after retries.
        """
        cached = self._cache.get(url)
        if cached is not None and cached[0] > self._clock():
            self._cache.move_to_end(url)
            return cached[1]

        live = not self.settings.offline and self._transport is None
        if live and not self.settings.user_agent:
            raise EdgarConfigError(
                f"Set {ENV_USER_AGENT} to identify yourself to the SEC, for"
                ' example "Jane Analyst jane@example.org". The SEC rejects'
                " automated requests that do not declare who is making them."
            )

        text = await self._fetch(url, rate_limited=live)
        self._cache[url] = (self._clock() + CACHE_TTL_SECONDS, text)
        self._cache.move_to_end(url)
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return text

    async def get_json(self, url: str) -> Any:
        """Returns the body of `url` parsed as JSON. See `get_text`."""
        return json.loads(await self.get_text(url))

    async def _fetch(self, url: str, *, rate_limited: bool) -> str:
        last_problem = "no attempt was made"
        for attempt in range(MAX_ATTEMPTS):
            if rate_limited:
                await self._limiter.acquire()
            self.requests_sent += 1
            try:
                response = await self._client().get(url)
            except httpx.TransportError as error:
                last_problem = f"{type(error).__name__}"
                await self._backoff(attempt, None)
                continue

            if response.status_code == 200:
                return response.text
            if response.status_code == 404:
                raise EdgarNotFoundError(url)
            if response.status_code == 403:
                raise EdgarConfigError(
                    "The SEC refused the request (HTTP 403). It blocks clients"
                    f" that do not declare themselves: check {ENV_USER_AGENT}."
                )
            last_problem = f"HTTP {response.status_code}"
            if response.status_code == 429 or response.status_code >= 500:
                await self._backoff(
                    attempt, response.headers.get("Retry-After")
                )
                continue
            break
        raise EdgarUnavailableError(
            f"SEC EDGAR did not answer successfully ({last_problem}) after"
            f" {MAX_ATTEMPTS} attempts."
        )

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt == MAX_ATTEMPTS - 1:
            return
        delay = BACKOFF_SECONDS * 2**attempt * (1 + random.random() / 2)
        if retry_after is not None:
            with contextlib.suppress(ValueError):
                delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        await self._sleep(delay)
