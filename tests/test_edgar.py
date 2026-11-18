"""Tests for company resolution, filing filters and figure selection."""

import pytest

from edgar_mcp.edgar import (
    METRICS,
    AmbiguousCompanyError,
    CompanyIndex,
    CompanyNotFoundError,
    InvalidArgumentError,
    PeriodKind,
    core_name,
    filter_filings,
    normalize_name,
    parse_filings,
    parse_profile,
    select_values,
)

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {
        "cik_str": 1067983,
        "ticker": "BRK-B",
        "title": "BERKSHIRE HATHAWAY INC",
    },
    "2": {
        "cik_str": 1067983,
        "ticker": "BRK-A",
        "title": "BERKSHIRE HATHAWAY INC",
    },
    "3": {
        "cik_str": 1418121,
        "ticker": "APLE",
        "title": "Apple Hospitality REIT, Inc.",
    },
    "4": {"cik_str": 91440, "ticker": "SNA", "title": "Snap-on Inc"},
    "5": {"cik_str": 11, "ticker": "ACM", "title": "Acme Widgets Corp"},
    "6": {"cik_str": 12, "ticker": "ACMH", "title": "Acme Holdings Corp"},
}
INDEX = CompanyIndex.from_tickers_json(TICKERS)


def test_name_normalization():
    assert normalize_name("Snap-on Inc.") == "snap on inc"
    assert normalize_name("AT&T INC.") == "at and t inc"
    assert core_name("The Walt Disney Co") == "walt disney"
    assert core_name("MICROSOFT CORP") == "microsoft"
    assert core_name("Co") == "co"


def test_index_groups_tickers_by_company():
    berkshire = INDEX.resolve("BRK-A")
    assert berkshire.tickers == ("BRK-B", "BRK-A")
    assert len(INDEX.companies) == 6


@pytest.mark.parametrize(
    ("query", "cik"),
    [
        ("320193", 320193),
        ("CIK0000320193", 320193),
        ("aapl", 320193),
        ("brk.b", 1067983),
        ("Apple Inc.", 320193),
        ("apple", 320193),
        ("Snap-on", 91440),
        ("snap on", 91440),
        ("Apple Hospitality", 1418121),
        ("Acme Widgets", 11),
    ],
)
def test_resolve(query, cik):
    assert INDEX.resolve(query).cik == cik


def test_resolve_reports_ambiguity_with_candidates():
    with pytest.raises(
        AmbiguousCompanyError, match=r"Acme Widgets Corp.*Acme Holdings Corp"
    ):
        INDEX.resolve("Acme")


def test_resolve_suggests_close_names():
    with pytest.raises(
        CompanyNotFoundError, match=r"Did you mean: Apple Inc\."
    ):
        INDEX.resolve("Appel")
    with pytest.raises(CompanyNotFoundError, match="No company has CIK 999"):
        INDEX.resolve("999")
    with pytest.raises(InvalidArgumentError):
        INDEX.resolve("  ")


def test_search_ranks_the_resolved_company_first():
    names = [c.name for c in INDEX.search("apple", 5)]
    assert names[:2] == ["Apple Inc.", "Apple Hospitality REIT, Inc."]
    assert [c.cik for c in INDEX.search("Acme", 5)] == [11, 12]
    assert INDEX.search("zzzz", 5) == []


SUBMISSIONS = {
    "cik": "0000000011",
    "name": "Acme Widgets Corp",
    "tickers": ["ACM"],
    "exchanges": ["NYSE", ""],
    "sic": "3990",
    "sicDescription": "Miscellaneous Manufacturing",
    "fiscalYearEnd": "0930",
    "stateOfIncorporation": "DE",
    "category": "Large accelerated filer",
    "filings": {
        "recent": {
            "accessionNumber": ["A-4", "A-3", "A-2", "A-1", "A-0"],
            "filingDate": [
                "2025-11-01",
                "2025-08-01",
                "2025-08-01",
                "2025-05-02",
                "2024-11-01",
            ],
            "reportDate": [
                "2025-09-30",
                "2025-06-30",
                "2025-07-30",
                "",
                "2024-09-30",
            ],
            "form": ["10-K", "10-Q", "8-K", "10-K/A", "10-K"],
            "primaryDocument": ["k.htm", "q.htm", "e.htm", "", "k0.htm"],
            "primaryDocDescription": ["10-K", "10-Q", "8-K", "", "10-K"],
            "items": ["", "", "2.02,9.01", "", ""],
        }
    },
}


def test_parse_profile_and_filings():
    profile = parse_profile(SUBMISSIONS)
    assert profile.fiscal_year_end == "09-30"
    assert profile.exchanges == ("NYSE",)
    filings = parse_filings(SUBMISSIONS)
    assert [f.accession_number for f in filings] == [
        "A-4",
        "A-3",
        "A-2",
        "A-1",
        "A-0",
    ]
    assert filings[2].items == "2.02,9.01"
    assert filings[3].report_date is None
    assert parse_filings({"filings": {}}) == []


def test_filter_filings():
    filings = parse_filings(SUBMISSIONS)

    def ids(**kwargs):
        return [f.accession_number for f in filter_filings(filings, **kwargs)]

    assert ids(form_types=["10-k"]) == ["A-4", "A-0"]
    assert ids(form_types=["10-K"], include_amendments=True) == [
        "A-4",
        "A-1",
        "A-0",
    ]
    assert ids(filed_after="2025-08-01", filed_before="2025-08-01") == [
        "A-3",
        "A-2",
    ]
    assert ids(form_types=[]) == ids()
    with pytest.raises(
        InvalidArgumentError, match="filed_after must be a date"
    ):
        ids(filed_after="2025-02-30")
    with pytest.raises(InvalidArgumentError, match="later than"):
        ids(filed_after="2025-09-01", filed_before="2025-01-01")


def fact(fy, fp, form, start, end, val, accn, filed):
    row = {
        "fy": fy,
        "fp": fp,
        "form": form,
        "end": end,
        "val": val,
        "accn": accn,
        "filed": filed,
    }
    if start:
        row["start"] = start
    return row


def concept(*facts, unit="USD"):
    return {"units": {unit: list(facts)}}


def test_select_values_takes_each_filings_own_period():
    # A 10-K for fiscal 2025 also reports 2024 and 2023, all tagged fy=2025.
    data = concept(
        fact(
            2025,
            "FY",
            "10-K",
            "2023-01-01",
            "2023-12-31",
            100,
            "K25",
            "2026-02-01",
        ),
        fact(
            2025,
            "FY",
            "10-K",
            "2024-01-01",
            "2024-12-31",
            200,
            "K25",
            "2026-02-01",
        ),
        fact(
            2025,
            "FY",
            "10-K",
            "2025-01-01",
            "2025-12-31",
            300,
            "K25",
            "2026-02-01",
        ),
        fact(
            2024,
            "FY",
            "10-K",
            "2024-01-01",
            "2024-12-31",
            190,
            "K24",
            "2025-02-01",
        ),
        fact(
            2025,
            "Q2",
            "10-Q",
            "2025-01-01",
            "2025-06-30",
            150,
            "Q25",
            "2025-08-01",
        ),
    )
    values = select_values([("Revenues", data)], METRICS["revenue"], "FY")
    assert [(v.fiscal_year, v.value) for v in values] == [
        (2025, 300),
        (2024, 190),
    ]
    assert values[0].period_start == "2025-01-01"
    assert values[0].accession_number == "K25"


def test_select_values_picks_the_quarter_not_year_to_date():
    data = concept(
        fact(
            2025,
            "Q2",
            "10-Q",
            "2025-01-01",
            "2025-06-30",
            150,
            "Q",
            "2025-08-01",
        ),
        fact(
            2025,
            "Q2",
            "10-Q",
            "2025-04-01",
            "2025-06-30",
            80,
            "Q",
            "2025-08-01",
        ),
        fact(
            2025,
            "Q2",
            "10-Q",
            "2024-04-01",
            "2024-06-30",
            70,
            "Q",
            "2025-08-01",
        ),
    )
    (value,) = select_values([("Revenues", data)], METRICS["revenue"], "Q2")
    assert (value.value, value.period_start) == (80, "2025-04-01")


def test_select_values_for_balance_sheet_figures():
    assert METRICS["total_assets"].kind is PeriodKind.INSTANT
    data = concept(
        fact(2025, "FY", "10-K", None, "2024-12-31", 900, "K", "2026-02-01"),
        fact(2025, "FY", "10-K", None, "2025-12-31", 1000, "K", "2026-02-01"),
        fact(
            2025, "FY", "10-K", "2025-01-01", "2025-12-31", 5, "K", "2026-02-01"
        ),
    )
    (value,) = select_values([("Assets", data)], METRICS["total_assets"], "FY")
    assert (value.value, value.period_end, value.period_start) == (
        1000,
        "2025-12-31",
        None,
    )


def test_select_values_prefers_earlier_tags_and_original_filings():
    old_tag = concept(
        fact(
            2018,
            "FY",
            "10-K",
            "2018-01-01",
            "2018-12-31",
            10,
            "K18",
            "2019-02-01",
        ),
        fact(
            2019,
            "FY",
            "10-K",
            "2019-01-01",
            "2019-12-31",
            11,
            "K19",
            "2020-02-01",
        ),
    )
    new_tag = concept(
        fact(
            2019,
            "FY",
            "10-K",
            "2019-01-01",
            "2019-12-31",
            99,
            "K19",
            "2020-02-01",
        ),
        fact(
            2020,
            "FY",
            "10-K",
            "2020-01-01",
            "2020-12-31",
            12,
            "K20",
            "2021-02-01",
        ),
        fact(
            2020,
            "FY",
            "10-K",
            "2020-01-01",
            "2020-12-31",
            13,
            "K20B",
            "2021-06-01",
        ),
    )
    values = select_values(
        [("Revenues", old_tag), ("SalesRevenueNet", new_tag)],
        METRICS["revenue"],
        "FY",
    )
    assert [(v.fiscal_year, v.value, v.tag) for v in values] == [
        (2020, 12, "SalesRevenueNet"),
        (2019, 11, "Revenues"),
        (2018, 10, "Revenues"),
    ]


def test_select_values_ignores_other_units_and_forms():
    data = concept(
        fact(
            2025,
            "FY",
            "10-K/A",
            "2025-01-01",
            "2025-12-31",
            1,
            "A",
            "2026-03-01",
        ),
        fact(
            2025, "FY", "8-K", "2025-01-01", "2025-12-31", 2, "E", "2026-01-15"
        ),
    )
    assert select_values([("Revenues", data)], METRICS["revenue"], "FY") == []
    per_share = concept(
        fact(
            2025,
            "FY",
            "10-K",
            "2025-01-01",
            "2025-12-31",
            6.5,
            "K",
            "2026-02-01",
        ),
        unit="USD/shares",
    )
    (value,) = select_values(
        [("EarningsPerShareDiluted", per_share)], METRICS["eps_diluted"], "FY"
    )
    assert (value.value, value.unit) == (6.5, "USD/shares")
