"""Protocol-level tests: a real MCP client talking to the server in-process."""

import asyncio
import gzip
import json

import pytest
from mcp import Client

from edgar_mcp.config import Settings, Toolset, bundled_snapshot
from edgar_mcp.edgar import METRICS
from edgar_mcp.server import build_server

SNAPSHOT = bundled_snapshot()
DESIGNED = build_server(Settings(snapshot_dir=SNAPSHOT))
BASELINE = build_server(
    Settings(snapshot_dir=SNAPSHOT, toolset=Toolset.BASELINE)
)


def call(name, arguments=None, server=DESIGNED):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})

    result = asyncio.run(go())
    return result.is_error, result.content[0].text


def ok(name, arguments=None):
    error, text = call(name, arguments)
    assert not error, text
    return json.loads(text)


def tools(server):
    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    return {t.name: t for t in asyncio.run(go())}


def test_designed_surface():
    listed = tools(DESIGNED)
    assert set(listed) == {
        "edgar_find_company",
        "edgar_company_profile",
        "edgar_list_filings",
        "edgar_get_financial_metric",
    }
    for tool in listed.values():
        assert tool.annotations.read_only_hint
        assert not tool.annotations.destructive_hint
        assert len(tool.description) > 80
        assert "company" in tool.input_schema["properties"] or tool.name == (
            "edgar_find_company"
        )
    metric = json.dumps(listed["edgar_get_financial_metric"].input_schema)
    assert all(f'"{key}"' in metric for key in METRICS)
    filings = listed["edgar_list_filings"].input_schema["properties"]
    assert "description" in filings["form_types"]


def test_baseline_surface_is_thin():
    assert set(tools(BASELINE)) == {
        "get_company_tickers",
        "get_submissions",
        "get_company_concept",
    }


def test_responses_are_compact_json():
    _, text = call("edgar_find_company", {"query": "MSFT"})
    assert ": " not in text
    assert ", " not in text.replace(", Inc", "")


def test_find_company():
    matches = ok("edgar_find_company", {"query": "apple"})["matches"]
    assert matches[0] == {
        "name": "Apple Inc.",
        "cik": 320193,
        "tickers": ["AAPL"],
    }
    assert "Apple Hospitality REIT, Inc." in [m["name"] for m in matches]


def test_company_profile():
    profile = ok("edgar_company_profile", {"company": "Hormel Foods"})
    assert profile["cik"] == 48465
    assert profile["fiscal_year_end"] == "10-25"
    assert profile["filings_indexed"]["count"] > 1000


def test_list_filings_counts_pages_and_details():
    first = ok(
        "edgar_list_filings",
        {"company": "AAPL", "form_types": ["10-K"], "limit": 2},
    )
    assert first["total_matching"] >= 10
    assert (first["returned"], first["next_offset"]) == (2, 2)
    assert set(first["filings"][0]) == {
        "form",
        "filing_date",
        "report_date",
        "accession_number",
    }
    second = ok(
        "edgar_list_filings",
        {"company": "AAPL", "form_types": ["10-K"], "limit": 2, "offset": 2},
    )
    assert (
        second["filings"][0]["filing_date"]
        < first["filings"][-1]["filing_date"]
    )
    past_end = ok(
        "edgar_list_filings",
        {"company": "AAPL", "form_types": ["10-K"], "offset": 999},
    )
    assert (past_end["returned"], past_end["next_offset"]) == (0, None)
    detailed = ok(
        "edgar_list_filings",
        {
            "company": "Etsy",
            "form_types": ["8-K"],
            "limit": 1,
            "response_format": "detailed",
        },
    )["filings"][0]
    assert detailed["url"].startswith(
        "https://www.sec.gov/Archives/edgar/data/1370637/"
    )
    assert detailed["items"]


def test_financial_metric():
    result = ok(
        "edgar_get_financial_metric",
        {
            "company": "Walmart",
            "metric": "operating_income",
            "fiscal_year": 2026,
        },
    )
    (value,) = result["values"]
    assert value["value"] == 29_825_000_000
    assert (value["period_start"], value["period_end"]) == (
        "2025-02-01",
        "2026-01-31",
    )
    assert value["form"] == "10-K"
    latest = ok(
        "edgar_get_financial_metric",
        {"company": "AAPL", "metric": "revenue", "limit": 3},
    )
    years = [v["fiscal_year"] for v in latest["values"]]
    assert years == sorted(years, reverse=True)
    assert len(years) == 3


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        (
            "edgar_list_filings",
            {"company": "Appel"},
            "Did you mean: Apple Inc.",
        ),
        (
            "edgar_list_filings",
            {"company": "Bank"},
            "matches several companies",
        ),
        (
            "edgar_list_filings",
            {"company": "MSFT", "filed_after": "2025-13-01"},
            "must be a date",
        ),
        (
            "edgar_get_financial_metric",
            {"company": "AAPL", "metric": "revenue", "fiscal_year": 1999},
            "Fiscal years available: 2009-",
        ),
        (
            "edgar_get_financial_metric",
            {"company": "WMT", "metric": "total_liabilities"},
            "reports no FY value of total liabilities",
        ),
        (
            "edgar_get_financial_metric",
            {"company": "AAPL", "metric": "ebitda"},
            "metric",
        ),
    ],
)
def test_errors_say_what_to_do(name, arguments, message):
    error, text = call(name, arguments)
    assert error
    assert message in text


def test_live_mode_without_user_agent_explains_itself():
    server = build_server(Settings())
    error, text = call("edgar_find_company", {"query": "AAPL"}, server)
    assert error
    assert "EDGAR_MCP_USER_AGENT" in text


def test_baseline_returns_the_raw_sec_documents():
    error, text = call("get_submissions", {"cik": "0000320193"}, BASELINE)
    raw = gzip.decompress(
        (
            SNAPSHOT / "data.sec.gov/submissions/CIK0000320193.json.gz"
        ).read_bytes()
    )
    assert not error
    assert text.encode() == raw
    error, text = call("get_submissions", {"cik": "320193"}, BASELINE)
    assert (error, text.endswith("HTTP 404")) == (True, True)
