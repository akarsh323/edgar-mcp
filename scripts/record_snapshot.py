"""Records a snapshot of SEC EDGAR responses for offline use and evaluation.

    export EDGAR_MCP_USER_AGENT="Your Name you@example.org"
    python scripts/record_snapshot.py

Writes the responses the server needs for a fixed set of companies, gzip
compressed and laid out like the SEC URLs, under
src/edgar_mcp/data/snapshot/, plus a manifest. Responses are stored exactly
as the SEC served them. The User-Agent is not recorded anywhere.

EDGAR data is public. Requests stay well under the SEC's rate ceiling.
"""

from __future__ import annotations

import asyncio
import datetime
import gzip
import json
import sys
from pathlib import Path

import httpx

from edgar_mcp.client import (
    TICKERS_URL,
    EdgarClient,
    EdgarNotFoundError,
    concept_url,
    submissions_url,
)
from edgar_mcp.config import ENV_USER_AGENT, Settings
from edgar_mcp.edgar import METRICS, CompanyIndex

COMPANIES = ("AAPL", "MSFT", "WMT", "ETSY", "SNA", "HRL")
ROOT = Path(__file__).resolve().parents[1] / "src/edgar_mcp/data/snapshot"


def write(url: str, text: str) -> int:
    """Stores one response; returns its compressed size."""
    parsed = httpx.URL(url)
    path = ROOT / parsed.host / (parsed.path.lstrip("/") + ".gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(text.encode("utf-8"), mtime=0)
    path.write_bytes(data)
    return len(data)


async def main() -> int:
    settings = Settings.from_env()
    if settings.offline or not settings.user_agent:
        print(f"Set {ENV_USER_AGENT} and unset the snapshot variable.")
        return 2
    client = EdgarClient(settings)
    try:
        tickers = await client.get_text(TICKERS_URL)
        total = write(TICKERS_URL, tickers)
        index = CompanyIndex.from_tickers_json(json.loads(tickers))
        tags = sorted({tag for spec in METRICS.values() for tag in spec.tags})
        companies, missing = [], []
        for ticker in COMPANIES:
            ref = index.resolve(ticker)
            companies.append(
                {"ticker": ticker, "cik": ref.cik, "name": ref.name}
            )
            url = submissions_url(ref.cik)
            total += write(url, await client.get_text(url))
            for tag in tags:
                url = concept_url(ref.cik, "us-gaap", tag)
                try:
                    total += write(url, await client.get_text(url))
                except EdgarNotFoundError:
                    missing.append(f"{ticker}:{tag}")
    finally:
        await client.aclose()

    manifest = {
        "recorded_on": datetime.date.today().isoformat(),
        "source": "SEC EDGAR public APIs (www.sec.gov, data.sec.gov)",
        "companies": companies,
        "tags_requested": tags,
        "tags_not_reported": missing,
        "requests": client.requests_sent,
        "compressed_bytes": total,
        "notes": (
            "Responses are stored byte for byte as served, gzip compressed."
            " Submissions documents include only the SEC's 'recent' block;"
            " older paged files are not recorded. A URL that is not in the"
            " snapshot is served as HTTP 404."
        ),
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"Recorded {client.requests_sent} responses"
        f" ({total / 1024:.0f} KiB compressed);"
        f" {len(missing)} tag(s) not reported."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
