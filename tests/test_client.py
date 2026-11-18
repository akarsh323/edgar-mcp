"""Tests for settings and the HTTP client. No test touches the network."""

import asyncio
import gzip

import httpx
import pytest

from edgar_mcp.client import (
    CACHE_TTL_SECONDS,
    MAX_ATTEMPTS,
    TICKERS_URL,
    EdgarClient,
    EdgarConfigError,
    EdgarNotFoundError,
    EdgarUnavailableError,
    RateLimiter,
    archive_url,
    concept_url,
    submissions_url,
)
from edgar_mcp.config import (
    ENV_RATE,
    ENV_SNAPSHOT,
    ENV_TOOLSET,
    ENV_USER_AGENT,
    Settings,
    Toolset,
    bundled_snapshot,
)

URL = "https://data.sec.gov/submissions/CIK0000000001.json"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def recorder(clock=None):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if clock is not None:
            clock.now += seconds

    return sleeps, sleep


def mock(responses, seen=None):
    queue = list(responses)

    def handler(request):
        if seen is not None:
            seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler)


def test_urls():
    assert submissions_url(320193) == (
        "https://data.sec.gov/submissions/CIK0000320193.json"
    )
    assert concept_url(320193, "us-gaap", "Assets") == (
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/Assets.json"
    )
    assert archive_url(320193, "0000320193-26-000026", "aapl.htm") == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/aapl.htm"
    )


def test_settings_from_env(tmp_path):
    settings = Settings.from_env(
        {
            ENV_USER_AGENT: " Jane jane@example.org ",
            ENV_SNAPSHOT: str(tmp_path),
            ENV_TOOLSET: "baseline",
            ENV_RATE: "2",
        }
    )
    assert settings.user_agent == "Jane jane@example.org"
    assert settings.snapshot_dir == tmp_path
    assert settings.offline
    assert settings.toolset is Toolset.BASELINE
    assert settings.max_requests_per_second == 2

    defaults = Settings.from_env({})
    assert (defaults.user_agent, defaults.offline) == (None, False)
    assert defaults.toolset is Toolset.DESIGNED
    assert Settings.from_env({ENV_SNAPSHOT: "bundled"}).snapshot_dir == (
        bundled_snapshot()
    )


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({ENV_SNAPSHOT: "/no/such/dir"}, "not a directory"),
        ({ENV_RATE: "11"}, "must be in"),
        ({ENV_RATE: "0"}, "must be in"),
    ],
)
def test_settings_reject_bad_values(env, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env)


def test_live_requests_need_a_declared_user_agent():
    client = EdgarClient(Settings())
    with pytest.raises(EdgarConfigError, match=ENV_USER_AGENT):
        asyncio.run(client.get_text(TICKERS_URL))
    assert client.requests_sent == 0


def test_snapshot_transport_serves_files_and_404s(tmp_path):
    folder = tmp_path / "data.sec.gov" / "submissions"
    folder.mkdir(parents=True)
    (folder / "CIK0000000001.json.gz").write_bytes(gzip.compress(b'{"a": 1}'))
    (folder / "CIK0000000002.json").write_bytes(b'{"b": 2}')
    client = EdgarClient(Settings(snapshot_dir=tmp_path))

    async def scenario():
        assert await client.get_json(URL) == {"a": 1}
        assert await client.get_json(URL.replace("1.json", "2.json")) == {
            "b": 2
        }
        with pytest.raises(EdgarNotFoundError):
            await client.get_text(URL.replace("1.json", "3.json"))
        await client.aclose()

    asyncio.run(scenario())


def test_sends_user_agent_and_caches():
    seen = []
    client = EdgarClient(
        Settings(user_agent="Test Agent test@example.org"),
        transport=mock([httpx.Response(200, json={"ok": True})], seen),
    )

    async def scenario():
        assert await client.get_json(URL) == {"ok": True}
        assert await client.get_json(URL) == {"ok": True}

    asyncio.run(scenario())
    assert len(seen) == 1
    assert seen[0].headers["User-Agent"] == "Test Agent test@example.org"


def test_cache_entries_expire():
    clock = FakeClock()
    client = EdgarClient(
        Settings(user_agent="x"),
        transport=mock(
            [httpx.Response(200, text="1"), httpx.Response(200, text="2")]
        ),
        clock=clock,
    )

    async def scenario():
        assert await client.get_text(URL) == "1"
        clock.now += CACHE_TTL_SECONDS + 1
        assert await client.get_text(URL) == "2"

    asyncio.run(scenario())


def test_retries_honor_retry_after_then_succeed():
    sleeps, sleep = recorder()
    client = EdgarClient(
        Settings(user_agent="x"),
        transport=mock(
            [
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.ConnectError("reset"),
                httpx.Response(200, text="ok"),
            ]
        ),
        sleep=sleep,
    )
    assert asyncio.run(client.get_text(URL)) == "ok"
    assert client.requests_sent == 3
    assert sleeps[0] == 2.0
    assert len(sleeps) == 2


def test_gives_up_after_the_last_attempt():
    sleeps, sleep = recorder()
    client = EdgarClient(
        Settings(user_agent="x"),
        transport=mock([httpx.Response(503)] * MAX_ATTEMPTS),
        sleep=sleep,
    )
    with pytest.raises(EdgarUnavailableError, match="HTTP 503"):
        asyncio.run(client.get_text(URL))
    assert client.requests_sent == MAX_ATTEMPTS
    assert len(sleeps) == MAX_ATTEMPTS - 1


def test_client_errors_are_not_retried():
    client = EdgarClient(
        Settings(user_agent="x"), transport=mock([httpx.Response(400)])
    )
    with pytest.raises(EdgarUnavailableError, match="HTTP 400"):
        asyncio.run(client.get_text(URL))
    assert client.requests_sent == 1


def test_forbidden_means_undeclared_client():
    client = EdgarClient(
        Settings(user_agent="x"), transport=mock([httpx.Response(403)])
    )
    with pytest.raises(EdgarConfigError, match="HTTP 403"):
        asyncio.run(client.get_text(URL))


def test_rate_limiter_spaces_request_starts():
    clock = FakeClock()
    sleeps, sleep = recorder(clock)
    limiter = RateLimiter(5, clock=clock, sleep=sleep)

    async def scenario():
        for _ in range(3):
            await limiter.acquire()

    asyncio.run(scenario())
    assert sleeps == pytest.approx([0.2, 0.2])
