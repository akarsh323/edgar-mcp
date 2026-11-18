"""EDGAR domain logic: company resolution, filing lists, reported figures.

The pure functions here turn the SEC's JSON into answers. `EdgarService`
fetches through `EdgarClient` and caches what is expensive to rebuild.

One rule deserves a note. In XBRL company-concept data, `fy` and `fp` are
the fiscal year and period *of the filing*, not of each value. A 10-K for
fiscal 2025 also reports 2024 and 2023 comparatives, and all three carry
`fy=2025`. Taking the first value with `fy=2025` therefore often returns an
older year. `select_values` takes, for each filing, the value whose period
ends last and whose length matches the period asked for.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import difflib
import enum
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from edgar_mcp.client import (
    TICKERS_URL,
    EdgarClient,
    EdgarNotFoundError,
    concept_url,
    submissions_url,
)

_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "plc",
        "llc",
        "lp",
        "sa",
        "nv",
        "ag",
        "se",
    }
)
_CIK_QUERY = re.compile(r"(?i)^(?:cik)?\s*0*(\d{1,10})$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_SUGGESTIONS = 5


class EdgarQueryError(Exception):
    """A request that cannot be answered as asked. The message says why."""


class CompanyNotFoundError(EdgarQueryError):
    """No company matches the query."""


class AmbiguousCompanyError(EdgarQueryError):
    """Several companies match the query."""


class MetricNotReportedError(EdgarQueryError):
    """The company does not report the figure for the period asked."""


class InvalidArgumentError(EdgarQueryError):
    """An argument is malformed."""


def normalize_name(name: str) -> str:
    """Lower-cases a company name and reduces punctuation to spaces."""
    text = name.casefold().replace("&", " and ")
    text = re.sub(r"[-/]", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.split())


def core_name(name: str) -> str:
    """Drops a leading "the" and trailing legal suffixes from a name."""
    tokens = normalize_name(name).split()
    if tokens[:1] == ["the"]:
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclasses.dataclass(frozen=True)
class CompanyRef:
    """A company registered with the SEC.

    Attributes:
      cik: Central Index Key.
      name: Registered name.
      tickers: Exchange tickers, primary first.
    """

    cik: int
    name: str
    tickers: tuple[str, ...]

    def describe(self) -> str:
        """Short human description, for error messages."""
        ticker = f", {self.tickers[0]}" if self.tickers else ""
        return f"{self.name} (CIK {self.cik}{ticker})"


class CompanyIndex:
    """Resolves tickers, names and CIKs to companies."""

    def __init__(self, companies: Sequence[CompanyRef]) -> None:
        """Creates an index. `companies` order is the tie-breaking order."""
        self.companies = list(companies)
        self._by_cik = {c.cik: c for c in self.companies}
        self._by_ticker = {t: c for c in self.companies for t in c.tickers}
        self._by_name: dict[str, list[CompanyRef]] = defaultdict(list)
        self._by_core: dict[str, list[CompanyRef]] = defaultdict(list)
        for company in self.companies:
            self._by_name[normalize_name(company.name)].append(company)
            self._by_core[core_name(company.name)].append(company)

    @classmethod
    def from_tickers_json(cls, data: dict[str, Any]) -> CompanyIndex:
        """Builds an index from the SEC's `company_tickers.json`."""
        names: dict[int, str] = {}
        tickers: dict[int, list[str]] = defaultdict(list)
        for key in sorted(data, key=int):
            entry = data[key]
            cik = int(entry["cik_str"])
            names.setdefault(cik, str(entry["title"]))
            tickers[cik].append(str(entry["ticker"]).upper())
        return cls(
            [CompanyRef(cik, names[cik], tuple(tickers[cik])) for cik in names]
        )

    def resolve(self, query: str) -> CompanyRef:
        """Returns the one company `query` identifies.

        Tries, in order: CIK, ticker, exact name, name without legal
        suffixes, and a unique name prefix.

        Raises:
          InvalidArgumentError: If the query is empty.
          AmbiguousCompanyError: If several companies match.
          CompanyNotFoundError: If none does. The message lists close names.
        """
        text = query.strip()
        if not text:
            raise InvalidArgumentError("company must not be empty.")

        cik = _CIK_QUERY.match(text)
        if cik:
            found = self._by_cik.get(int(cik.group(1)))
            if found is None:
                raise CompanyNotFoundError(
                    f"No company has CIK {int(cik.group(1))}."
                )
            return found

        ticker = self._by_ticker.get(text.upper().replace(".", "-"))
        if ticker is not None:
            return ticker

        for table, key in (
            (self._by_name, normalize_name(text)),
            (self._by_core, core_name(text)),
        ):
            matches = _unique(table.get(key, []))
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise _ambiguous(text, matches)

        prefixed = self._prefix_matches(core_name(text))
        if len(prefixed) == 1:
            return prefixed[0]
        if prefixed:
            raise _ambiguous(text, prefixed)

        suggestions = self.suggest(text)
        hint = (
            " Did you mean: "
            + "; ".join(c.describe() for c in suggestions)
            + "?"
            if suggestions
            else " Try a ticker such as AAPL, or edgar_find_company."
        )
        raise CompanyNotFoundError(f"No company matches {text!r}.{hint}")

    def search(self, query: str, limit: int) -> list[CompanyRef]:
        """Returns up to `limit` companies that match `query`, best first."""
        try:
            first = [self.resolve(query)]
        except AmbiguousCompanyError:
            first = []
        except EdgarQueryError:
            return self.suggest(query)[:limit]
        prefixed = self._prefix_matches(core_name(query))
        return _unique([*first, *prefixed, *self.suggest(query)])[:limit]

    def suggest(self, query: str) -> list[CompanyRef]:
        """Returns companies with names or tickers close to `query`."""
        cores = {core_name(c.name): c for c in reversed(self.companies)}
        close = difflib.get_close_matches(
            core_name(query), list(cores), n=MAX_SUGGESTIONS, cutoff=0.8
        )
        found = [cores[name] for name in close]
        ticker = difflib.get_close_matches(
            query.strip().upper(), list(self._by_ticker), n=2, cutoff=0.8
        )
        found += [self._by_ticker[t] for t in ticker]
        return _unique(found)[:MAX_SUGGESTIONS]

    def _prefix_matches(self, core: str) -> list[CompanyRef]:
        if not core:
            return []
        pattern = re.compile(rf"^{re.escape(core)}\b")
        return _unique(
            c for c in self.companies if pattern.match(core_name(c.name))
        )


def _unique(companies: Iterable[CompanyRef]) -> list[CompanyRef]:
    seen: dict[int, CompanyRef] = {}
    for company in companies:
        seen.setdefault(company.cik, company)
    return list(seen.values())


def _ambiguous(
    query: str, matches: Sequence[CompanyRef]
) -> AmbiguousCompanyError:
    listed = "; ".join(c.describe() for c in matches[:MAX_SUGGESTIONS])
    more = len(matches) - MAX_SUGGESTIONS
    tail = f"; and {more} more" if more > 0 else ""
    return AmbiguousCompanyError(
        f"{query!r} matches several companies: {listed}{tail}. Use a ticker"
        " or CIK to pick one."
    )


@dataclasses.dataclass(frozen=True)
class Filing:
    """One filing from a company's EDGAR index.

    Attributes:
      accession_number: EDGAR accession number, e.g. 0000320193-25-000079.
      form: Form type, e.g. 10-K.
      filing_date: ISO date the filing was made.
      report_date: ISO end date of the period it reports on, if any.
      primary_document: File name of the main document, if any.
      description: The SEC's description of the main document, if any.
      items: For 8-Ks, the item numbers reported, e.g. "2.02,9.01".
    """

    accession_number: str
    form: str
    filing_date: str
    report_date: str | None
    primary_document: str | None
    description: str | None
    items: str | None


@dataclasses.dataclass(frozen=True)
class CompanyProfile:
    """Registration details from a company's EDGAR index.

    Attributes:
      cik: Central Index Key.
      name: Registered name.
      tickers: Exchange tickers.
      exchanges: Exchanges the tickers trade on.
      sic: Standard Industrial Classification code.
      sic_description: Industry description for the SIC code.
      fiscal_year_end: Month and day the fiscal year ends, as MM-DD.
      state_of_incorporation: State or country code of incorporation.
      category: The SEC's filer category.
    """

    cik: int
    name: str
    tickers: tuple[str, ...]
    exchanges: tuple[str, ...]
    sic: str | None
    sic_description: str | None
    fiscal_year_end: str | None
    state_of_incorporation: str | None
    category: str | None


def parse_profile(submissions: dict[str, Any]) -> CompanyProfile:
    """Extracts registration details from a submissions document."""
    fye = submissions.get("fiscalYearEnd") or None
    return CompanyProfile(
        cik=int(submissions["cik"]),
        name=str(submissions.get("name", "")),
        tickers=tuple(submissions.get("tickers") or ()),
        exchanges=tuple(x for x in submissions.get("exchanges") or () if x),
        sic=submissions.get("sic") or None,
        sic_description=submissions.get("sicDescription") or None,
        fiscal_year_end=f"{fye[:2]}-{fye[2:]}"
        if fye and len(fye) == 4
        else None,
        state_of_incorporation=submissions.get("stateOfIncorporation") or None,
        category=submissions.get("category") or None,
    )


def parse_filings(submissions: dict[str, Any]) -> list[Filing]:
    """Extracts the recent-filings index, newest first.

    The SEC's `recent` block holds at least the last year of filings or the
    last 1,000, whichever is more. Older filings live in separate pages that
    this function does not read.
    """
    recent = submissions.get("filings", {}).get("recent", {})

    def column(name: str) -> list[Any]:
        values = recent.get(name)
        return values if isinstance(values, list) else []

    accession = column("accessionNumber")
    forms, filed, reported = (
        column("form"),
        column("filingDate"),
        column("reportDate"),
    )
    documents, descriptions, items = (
        column("primaryDocument"),
        column("primaryDocDescription"),
        column("items"),
    )

    def cell(values: list[Any], i: int) -> str | None:
        return str(values[i]) if i < len(values) and values[i] else None

    filings = [
        Filing(
            accession_number=str(accession[i]),
            form=cell(forms, i) or "",
            filing_date=cell(filed, i) or "",
            report_date=cell(reported, i),
            primary_document=cell(documents, i),
            description=cell(descriptions, i),
            items=cell(items, i),
        )
        for i in range(len(accession))
    ]
    filings.sort(
        key=lambda f: (f.filing_date, f.accession_number), reverse=True
    )
    return filings


def check_date(name: str, value: str | None) -> str | None:
    """Validates an optional ISO date argument.

    Raises:
      InvalidArgumentError: If `value` is not a real YYYY-MM-DD date.
    """
    if value is None or value == "":
        return None
    text = value.strip()
    try:
        if not _ISO_DATE.match(text):
            raise ValueError(text)
        datetime.date.fromisoformat(text)
    except ValueError:
        raise InvalidArgumentError(
            f"{name} must be a date like 2025-01-31, got {value!r}."
        ) from None
    return text


def filter_filings(
    filings: Sequence[Filing],
    *,
    form_types: Sequence[str] | None = None,
    filed_after: str | None = None,
    filed_before: str | None = None,
    include_amendments: bool = False,
) -> list[Filing]:
    """Filters filings by form and by an inclusive filing-date range.

    Args:
      filings: Filings to filter, in the order to keep.
      form_types: Forms to keep, e.g. ["10-K"]. Case-insensitive. `None` or
        an empty list keeps every form.
      filed_after: Keep filings made on or after this ISO date.
      filed_before: Keep filings made on or before this ISO date.
      include_amendments: Also keep amendments ("10-K/A" for "10-K").

    Returns:
      The matching filings.

    Raises:
      InvalidArgumentError: If a date is malformed or the range is empty.
    """
    after = check_date("filed_after", filed_after)
    before = check_date("filed_before", filed_before)
    if after and before and after > before:
        raise InvalidArgumentError("filed_after is later than filed_before.")
    wanted = {f.strip().upper() for f in form_types or () if f.strip()}
    if include_amendments:
        wanted |= {f"{f}/A" for f in wanted}
    return [
        f
        for f in filings
        if (not wanted or f.form.upper() in wanted)
        and (after is None or f.filing_date >= after)
        and (before is None or f.filing_date <= before)
    ]


class PeriodKind(enum.StrEnum):
    """Whether a figure covers a span of time or a point in time."""

    DURATION = "duration"
    INSTANT = "instant"


@dataclasses.dataclass(frozen=True)
class MetricSpec:
    """A financial figure and the XBRL tags it is reported under.

    Attributes:
      key: Name used in tool calls.
      label: Human description.
      tags: us-gaap tags, most preferred first. Companies switch tags over
        time (revenue moved to ASC 606 tags in 2018), so a figure is taken
        from the first tag that reports it for the period.
      kind: Span of time or point in time.
      unit: XBRL unit the value is reported in.
    """

    key: str
    label: str
    tags: tuple[str, ...]
    kind: PeriodKind
    unit: str = "USD"
    taxonomy: str = "us-gaap"


METRICS: dict[str, MetricSpec] = {
    spec.key: spec
    for spec in (
        MetricSpec(
            "revenue",
            "revenue",
            (
                "Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet",
            ),
            PeriodKind.DURATION,
        ),
        MetricSpec(
            "net_income", "net income", ("NetIncomeLoss",), PeriodKind.DURATION
        ),
        MetricSpec(
            "operating_income",
            "operating income",
            ("OperatingIncomeLoss",),
            PeriodKind.DURATION,
        ),
        MetricSpec(
            "research_and_development",
            "research and development expense",
            ("ResearchAndDevelopmentExpense",),
            PeriodKind.DURATION,
        ),
        MetricSpec(
            "eps_diluted",
            "diluted earnings per share",
            ("EarningsPerShareDiluted",),
            PeriodKind.DURATION,
            unit="USD/shares",
        ),
        MetricSpec(
            "total_assets", "total assets", ("Assets",), PeriodKind.INSTANT
        ),
        MetricSpec(
            "total_liabilities",
            "total liabilities",
            ("Liabilities",),
            PeriodKind.INSTANT,
        ),
        MetricSpec(
            "cash_and_equivalents",
            "cash and cash equivalents",
            ("CashAndCashEquivalentsAtCarryingValue",),
            PeriodKind.INSTANT,
        ),
    )
}

FISCAL_PERIODS = ("FY", "Q1", "Q2", "Q3")
_DAYS = {"FY": (350, 380), "Q1": (80, 100), "Q2": (80, 100), "Q3": (80, 100)}


@dataclasses.dataclass(frozen=True)
class ReportedValue:
    """One figure as a filing reported it for its own period.

    Attributes:
      value: The number, in `unit`.
      unit: XBRL unit, e.g. USD.
      fiscal_year: Fiscal year of the filing, as the company labels it.
      fiscal_period: FY, Q1, Q2 or Q3.
      period_start: First day of the period, for spans of time.
      period_end: Last day of the period, or the balance-sheet date.
      form: Form the value was reported in.
      filed: ISO date the filing was made.
      accession_number: Accession number of the filing.
      tag: XBRL tag the value was reported under.
    """

    value: int | float
    unit: str
    fiscal_year: int
    fiscal_period: str
    period_start: str | None
    period_end: str
    form: str
    filed: str
    accession_number: str
    tag: str


def _days(fact: dict[str, Any]) -> int | None:
    if "start" not in fact:
        return None
    start = datetime.date.fromisoformat(fact["start"])
    end = datetime.date.fromisoformat(fact["end"])
    return (end - start).days


def _fits(fact: dict[str, Any], kind: PeriodKind, fiscal_period: str) -> bool:
    days = _days(fact)
    if kind is PeriodKind.INSTANT:
        return days is None
    low, high = _DAYS[fiscal_period]
    return days is not None and low <= days <= high


def select_values(
    concepts: Sequence[tuple[str, dict[str, Any]]],
    spec: MetricSpec,
    fiscal_period: str,
) -> list[ReportedValue]:
    """Picks each filing's own-period value of a figure.

    Args:
      concepts: (tag, company-concept JSON) pairs, most preferred tag first.
      spec: The figure.
      fiscal_period: FY (from 10-Ks) or Q1, Q2, Q3 (from 10-Qs).

    Returns:
      One value per fiscal year, newest first. For each filing, the value is
      the one whose period ends last and whose length matches the period
      (about a year for FY, about a quarter for Q1-Q3, a single date for
      balance-sheet figures). Across tags, the most preferred tag wins; for
      one tag, the earliest filing for a fiscal year wins, so amendments and
      restatements do not replace the figure as first reported.
    """
    form = "10-K" if fiscal_period == "FY" else "10-Q"
    best: dict[int, tuple[int, ReportedValue]] = {}
    for priority, (tag, data) in enumerate(concepts):
        filings: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        for fact in data.get("units", {}).get(spec.unit, []):
            if (
                fact.get("form") == form
                and fact.get("fp") == fiscal_period
                and isinstance(fact.get("fy"), int)
                and _fits(fact, spec.kind, fiscal_period)
            ):
                filings[(fact["fy"], fact["accn"])].append(fact)
        for (fiscal_year, accession), facts in filings.items():
            last = max(f["end"] for f in facts)
            fact = min(
                (f for f in facts if f["end"] == last),
                key=lambda f: (
                    f.get("filed", ""),
                    f.get("start", ""),
                    f["val"],
                ),
            )
            candidate = ReportedValue(
                value=fact["val"],
                unit=spec.unit,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                period_start=fact.get("start"),
                period_end=fact["end"],
                form=form,
                filed=fact.get("filed", ""),
                accession_number=accession,
                tag=tag,
            )
            current = best.get(fiscal_year)
            if current is None or (priority, candidate.filed) < (
                current[0],
                current[1].filed,
            ):
                best[fiscal_year] = (priority, candidate)
    return [best[year][1] for year in sorted(best, reverse=True)]


class EdgarService:
    """EDGAR queries for the tool layer, with parsed results cached."""

    def __init__(self, client: EdgarClient) -> None:
        """Creates a service on top of `client`."""
        self.client = client
        self._index: CompanyIndex | None = None
        self._index_lock = asyncio.Lock()

    async def index(self) -> CompanyIndex:
        """Returns the company index, fetching it on first use."""
        async with self._index_lock:
            if self._index is None:
                data = await self.client.get_json(TICKERS_URL)
                self._index = CompanyIndex.from_tickers_json(data)
            return self._index

    async def resolve(self, company: str) -> CompanyRef:
        """Resolves a ticker, name or CIK. See `CompanyIndex.resolve`."""
        return (await self.index()).resolve(company)

    async def submissions(self, cik: int) -> dict[str, Any]:
        """Returns the raw submissions document of a company."""
        data: dict[str, Any] = await self.client.get_json(submissions_url(cik))
        return data

    async def profile(
        self, company: str
    ) -> tuple[CompanyProfile, list[Filing]]:
        """Returns a company's registration details and recent filings."""
        ref = await self.resolve(company)
        data = await self.submissions(ref.cik)
        return parse_profile(data), parse_filings(data)

    async def filings(self, company: str) -> tuple[CompanyRef, list[Filing]]:
        """Returns a company and its recent filings, newest first."""
        ref = await self.resolve(company)
        return ref, parse_filings(await self.submissions(ref.cik))

    async def metric(
        self,
        company: str,
        metric: str,
        fiscal_period: str = "FY",
    ) -> tuple[CompanyRef, MetricSpec, list[ReportedValue]]:
        """Returns every fiscal year's value of a figure, newest first.

        Raises:
          InvalidArgumentError: If the metric or period is unknown.
          MetricNotReportedError: If the company reports no value at all.
        """
        spec = METRICS.get(metric)
        if spec is None:
            raise InvalidArgumentError(
                f"Unknown metric {metric!r}. Use one of: {', '.join(METRICS)}."
            )
        if fiscal_period not in FISCAL_PERIODS:
            raise InvalidArgumentError(
                f"fiscal_period must be one of {', '.join(FISCAL_PERIODS)}."
            )
        ref = await self.resolve(company)
        concepts = []
        for tag in spec.tags:
            try:
                data = await self.client.get_json(
                    concept_url(ref.cik, spec.taxonomy, tag)
                )
            except EdgarNotFoundError:
                continue
            concepts.append((tag, data))
        values = select_values(concepts, spec, fiscal_period)
        if not values:
            raise MetricNotReportedError(
                f"{ref.name} reports no {fiscal_period} value of {spec.label}"
                f" under the XBRL tags {', '.join(spec.tags)}."
            )
        return ref, spec, values
