"""The MCP server and its two tool surfaces.

`designed` is the surface this project recommends. It follows the guidance
in Anthropic's "Writing effective tools for agents": a few task-shaped tools
instead of one per endpoint, names under one prefix, identifiers people use
(tickers and names, not zero-padded CIKs), filtering and paging so responses
stay small, a `response_format` switch, and errors that say what to do next.

`baseline` is a thin wrapper over the raw SEC endpoints, the shape many API
wrappers take. It exists so the difference can be measured
(`edgar_mcp.evals`), not to be used.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from edgar_mcp import __version__
from edgar_mcp.client import (
    SEC_DATA,
    TICKERS_URL,
    EdgarClient,
    EdgarConfigError,
    EdgarError,
    EdgarNotFoundError,
    archive_url,
)
from edgar_mcp.config import Settings, Toolset
from edgar_mcp.edgar import (
    METRICS,
    CompanyRef,
    EdgarQueryError,
    EdgarService,
    Filing,
    MetricNotReportedError,
    filter_filings,
)

INSTRUCTIONS = """\
Tools for U.S. SEC EDGAR: company lookup, filing history and reported \
financial figures. Every tool takes a ticker (AAPL), a company name \
(Apple Inc.) or an SEC CIK (320193) as `company`. Dates are ISO \
(YYYY-MM-DD). Figures come from the companies' own XBRL financial data."""

MetricName = Literal[
    "revenue",
    "net_income",
    "operating_income",
    "research_and_development",
    "eps_diluted",
    "total_assets",
    "total_liabilities",
    "cash_and_equivalents",
]
CompanyArg = Annotated[
    str,
    Field(
        description=(
            "Ticker (AAPL), company name (Apple Inc.) or SEC CIK (320193)."
        )
    ),
]
_READ_ONLY = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": True,
}


def _annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, **_READ_ONLY)


def _dumps(payload: Any) -> str:
    """Compact JSON: tool output is paid for in tokens."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _company(ref: CompanyRef) -> dict[str, Any]:
    return {"name": ref.name, "cik": ref.cik, "tickers": list(ref.tickers)}


async def _answer(work: Callable[[], Awaitable[str]]) -> str:
    """Runs a tool body and turns domain failures into actionable errors."""
    try:
        return await work()
    except EdgarQueryError as error:
        raise ToolError(str(error)) from error
    except EdgarConfigError as error:
        raise ToolError(str(error)) from error
    except EdgarNotFoundError as error:
        raise ToolError(f"EDGAR has no data at {error}.") from error
    except EdgarError as error:
        raise ToolError(f"{error} Try again in a minute.") from error


def build_server(
    settings: Settings | None = None, *, service: EdgarService | None = None
) -> MCPServer:
    """Builds the server for the configured tool surface.

    Args:
      settings: Settings. Defaults to `Settings.from_env()`.
      service: Service to use, for tests. Defaults to one built from
        `settings`.

    Returns:
      The server, ready to `run()`.
    """
    settings = settings or Settings.from_env()
    service = service or EdgarService(EdgarClient(settings))
    server = MCPServer(
        name="edgar",
        title="SEC EDGAR",
        instructions=INSTRUCTIONS,
        version=__version__,
    )
    if settings.toolset is Toolset.DESIGNED:
        _register_designed(server, service)
    else:
        _register_baseline(server, service.client)
    return server


def _filing(filing: Filing, cik: int, detailed: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "form": filing.form,
        "filing_date": filing.filing_date,
        "report_date": filing.report_date,
        "accession_number": filing.accession_number,
    }
    if detailed:
        row["description"] = filing.description
        row["items"] = filing.items
        row["url"] = (
            archive_url(cik, filing.accession_number, filing.primary_document)
            if filing.primary_document
            else None
        )
    return row


def _register_designed(server: MCPServer, service: EdgarService) -> None:
    @server.tool(
        name="edgar_find_company",
        annotations=_annotations("Find a company"),
    )
    async def find_company(
        query: Annotated[
            str, Field(description="Ticker, full or partial name, or CIK.")
        ],
        limit: Annotated[int, Field(ge=1, le=25)] = 5,
    ) -> str:
        """Find companies registered with the SEC, best match first.

        Use this when a name could mean more than one company. The other
        edgar_ tools accept a ticker, name or CIK directly, so there is no
        need to call this first when the company is clear.
        """

        async def work() -> str:
            matches = (await service.index()).search(query, limit)
            return _dumps(
                {"query": query, "matches": [_company(m) for m in matches]}
            )

        return await _answer(work)

    @server.tool(
        name="edgar_company_profile",
        annotations=_annotations("Company profile"),
    )
    async def company_profile(company: CompanyArg) -> str:
        """Get a company's SEC registration details.

        Returns the registered name, CIK, tickers and exchanges, industry
        (SIC code and description), fiscal year end (MM-DD), state of
        incorporation, filer category, and the date range of filings that
        edgar_list_filings can search.
        """

        async def work() -> str:
            profile, filings = await service.profile(company)
            return _dumps(
                {
                    "name": profile.name,
                    "cik": profile.cik,
                    "tickers": list(profile.tickers),
                    "exchanges": list(profile.exchanges),
                    "industry": {
                        "sic": profile.sic,
                        "description": profile.sic_description,
                    },
                    "fiscal_year_end": profile.fiscal_year_end,
                    "state_of_incorporation": profile.state_of_incorporation,
                    "filer_category": profile.category,
                    "filings_indexed": {
                        "count": len(filings),
                        "from": filings[-1].filing_date if filings else None,
                        "to": filings[0].filing_date if filings else None,
                    },
                }
            )

        return await _answer(work)

    @server.tool(
        name="edgar_list_filings",
        annotations=_annotations("List filings"),
    )
    async def list_filings(
        company: CompanyArg,
        form_types: Annotated[
            list[str] | None,
            Field(
                description=(
                    'Forms to include, e.g. ["10-K"] (annual report),'
                    ' ["10-Q"] (quarterly report), ["8-K"] (current report),'
                    ' ["DEF 14A"] (proxy statement). Omit for all forms.'
                )
            ),
        ] = None,
        filed_after: Annotated[
            str | None,
            Field(description="Only filings made on or after this date."),
        ] = None,
        filed_before: Annotated[
            str | None,
            Field(description="Only filings made on or before this date."),
        ] = None,
        include_amendments: Annotated[
            bool,
            Field(description="Also include amendments, e.g. 10-K/A."),
        ] = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        offset: Annotated[int, Field(ge=0)] = 0,
        response_format: Annotated[
            Literal["concise", "detailed"],
            Field(
                description=(
                    "concise: form, dates and accession number. detailed:"
                    " also the document description, 8-K items and a link."
                )
            ),
        ] = "concise",
    ) -> str:
        """List a company's SEC filings, newest first.

        `total_matching` counts every filing that matches the filters, so
        a count needs no paging. When more filings match than were
        returned, call again with `offset` set to `next_offset`. Covers the
        company's recent-filings index: at least the last year of filings
        or the last 1,000, whichever is more.
        """

        async def work() -> str:
            ref, filings = await service.filings(company)
            matching = filter_filings(
                filings,
                form_types=form_types,
                filed_after=filed_after,
                filed_before=filed_before,
                include_amendments=include_amendments,
            )
            page = matching[offset : offset + limit]
            more = offset + len(page) < len(matching)
            detailed = response_format == "detailed"
            return _dumps(
                {
                    "company": _company(ref),
                    "total_matching": len(matching),
                    "offset": offset,
                    "returned": len(page),
                    "next_offset": offset + len(page) if more else None,
                    "filings": [_filing(f, ref.cik, detailed) for f in page],
                }
            )

        return await _answer(work)

    @server.tool(
        name="edgar_get_financial_metric",
        annotations=_annotations("Get a financial figure"),
    )
    async def get_financial_metric(
        company: CompanyArg,
        metric: Annotated[
            MetricName,
            Field(
                description=(
                    "The figure. Amounts are in USD; eps_diluted is USD per"
                    " share. Balance-sheet figures (total_assets,"
                    " total_liabilities, cash_and_equivalents) are as of the"
                    " period end."
                )
            ),
        ],
        fiscal_year: Annotated[
            int | None,
            Field(
                description=(
                    "Fiscal year as the company labels it. Omit for the most"
                    " recent years."
                )
            ),
        ] = None,
        fiscal_period: Annotated[
            Literal["FY", "Q1", "Q2", "Q3"],
            Field(description="FY for the 10-K figure, Q1-Q3 for a 10-Q."),
        ] = "FY",
        limit: Annotated[int, Field(ge=1, le=20)] = 4,
    ) -> str:
        """Get a financial figure as a company reported it for a period.

        Returns the value each annual (10-K) or quarterly (10-Q) report gives
        for its own period, not the prior-period comparatives the same
        report also contains, with the period dates, filing date and
        accession number so the figure can be cited.
        """

        async def work() -> str:
            ref, spec, values = await service.metric(
                company, metric, fiscal_period
            )
            years = [v.fiscal_year for v in values]
            if fiscal_year is not None:
                values = [v for v in values if v.fiscal_year == fiscal_year]
                if not values:
                    raise MetricNotReportedError(
                        f"{ref.name} has no {fiscal_period} value of"
                        f" {spec.label} for fiscal year {fiscal_year}."
                        f" Fiscal years available: {_span(years)}."
                    )
            return _dumps(
                {
                    "company": _company(ref),
                    "metric": spec.key,
                    "label": spec.label,
                    "unit": spec.unit,
                    "fiscal_period": fiscal_period,
                    "values": [
                        {
                            "fiscal_year": v.fiscal_year,
                            "value": v.value,
                            "period_start": v.period_start,
                            "period_end": v.period_end,
                            "form": v.form,
                            "filed": v.filed,
                            "accession_number": v.accession_number,
                            "xbrl_tag": v.tag,
                        }
                        for v in values[:limit]
                    ],
                    "available_fiscal_years": _span(years),
                }
            )

        return await _answer(work)


def _span(years: list[int]) -> str:
    if not years:
        return "none"
    return f"{min(years)}-{max(years)}" if len(years) > 1 else str(years[0])


def _register_baseline(server: MCPServer, client: EdgarClient) -> None:
    async def raw(url: str) -> str:
        try:
            return await client.get_text(url)
        except EdgarNotFoundError as error:
            raise ToolError("HTTP 404") from error
        except EdgarError as error:
            raise ToolError(str(error)) from error

    @server.tool(name="get_company_tickers")
    async def get_company_tickers() -> str:
        """Get the SEC company tickers JSON."""
        return await raw(TICKERS_URL)

    @server.tool(name="get_submissions")
    async def get_submissions(cik: str) -> str:
        """Get the SEC submissions JSON for a CIK (10 digits, zero-padded)."""
        return await raw(f"{SEC_DATA}/submissions/CIK{cik}.json")

    @server.tool(name="get_company_concept")
    async def get_company_concept(cik: str, taxonomy: str, tag: str) -> str:
        """Get the SEC XBRL company concept JSON for a CIK, taxonomy and tag."""
        return await raw(
            f"{SEC_DATA}/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
        )


assert set(get_args(MetricName)) == set(METRICS), "MetricName out of sync"
