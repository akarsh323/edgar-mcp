"""Evaluation questions, generated from the snapshot with their answers.

Every answer is computed here, from the raw snapshot files, without going
through the server's code. Where the server picks a fiscal year's figure by
fiscal-year grouping, this module picks it by the accession number of the
10-K. The two routes are independent, so `tests/test_questions.py` checking
that the server agrees with every answer is a real cross-check.

Questions ask for things that change with each filing (accession numbers,
filing dates, counts, figures for fiscal years after current model training
cut-offs), so they cannot be answered from memory.
"""

from __future__ import annotations

import datetime
import enum
import gzip
import json
from collections.abc import Iterable
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from edgar_mcp.config import bundled_snapshot
from edgar_mcp.edgar import METRICS, PeriodKind

DATASET = "tasks_v1.jsonl"
COUNT_WINDOW = ("2025-01-01", "2025-06-30")
METRIC_ROTATION = (
    "total_liabilities",
    "cash_and_equivalents",
    "operating_income",
    "research_and_development",
)
DISPLAY_NAMES = {
    "AAPL": "Apple Inc.",
    "MSFT": "Microsoft",
    "WMT": "Walmart",
    "ETSY": "Etsy",
    "SNA": "Snap-on",
    "HRL": "Hormel Foods",
}


class Category(enum.StrEnum):
    """What a question asks for."""

    LATEST_ANNUAL_REPORT = "latest_annual_report"
    QUARTERLY_FILING_DATE = "quarterly_filing_date"
    CURRENT_REPORT_COUNT = "current_report_count"
    ANNUAL_FIGURE = "annual_figure"


class AnswerKind(enum.StrEnum):
    """How an answer is normalized for comparison."""

    ACCESSION = "accession"
    DATE = "date"
    COUNT = "count"
    AMOUNT = "amount"


ANSWER_KIND = {
    Category.LATEST_ANNUAL_REPORT: AnswerKind.ACCESSION,
    Category.QUARTERLY_FILING_DATE: AnswerKind.DATE,
    Category.CURRENT_REPORT_COUNT: AnswerKind.COUNT,
    Category.ANNUAL_FIGURE: AnswerKind.AMOUNT,
}


class Question(BaseModel):
    """One evaluation question.

    Attributes:
      task_id: Stable id, e.g. `AAPL-latest_annual_report`.
      category: What is asked.
      ticker: Ticker of the company asked about.
      company: Name used in the question.
      question: The question text.
      answer: The correct answer, in canonical form.
      kind: How answers are normalized.
      evidence: Where the answer comes from, for auditing.
      metric: Metric key, for annual-figure questions.
      fiscal_year: Fiscal year, for annual-figure questions.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    category: Category
    ticker: str
    company: str
    question: str
    answer: str
    kind: AnswerKind
    evidence: str
    metric: str | None = None
    fiscal_year: int | None = None


def _load(root: Path, relative: str) -> Any:
    path = root / (relative + ".gz")
    if not path.is_file():
        return None
    return json.loads(gzip.decompress(path.read_bytes()))


def _rows(submissions: dict[str, Any]) -> list[dict[str, str]]:
    recent = submissions["filings"]["recent"]
    columns = ("accessionNumber", "filingDate", "reportDate", "form")
    return [
        {name: recent[name][i] for name in columns}
        for i in range(len(recent["accessionNumber"]))
    ]


def _days(fact: dict[str, Any]) -> int | None:
    if "start" not in fact:
        return None
    start = datetime.date.fromisoformat(fact["start"])
    return (datetime.date.fromisoformat(fact["end"]) - start).days


def _figure(
    root: Path, cik: int, key: str, accession: str
) -> tuple[int, int, str] | None:
    """Finds the current-period value of a figure in one 10-K.

    Returns:
      (value, fiscal year, tag), or `None` if the 10-K does not report it.
    """
    spec = METRICS[key]
    for tag in spec.tags:
        data = _load(
            root,
            f"data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json",
        )
        if data is None:
            continue
        facts = [
            f
            for f in data["units"].get(spec.unit, [])
            if f.get("accn") == accession and f.get("form") == "10-K"
        ]
        if spec.kind is PeriodKind.INSTANT:
            facts = [f for f in facts if _days(f) is None]
        else:
            facts = [f for f in facts if 350 <= (_days(f) or 0) <= 380]
        if facts:
            fact = max(facts, key=lambda f: f["end"])
            return int(fact["val"]), int(fact["fy"]), tag
    return None


def generate_questions(root: Path | None = None) -> list[Question]:
    """Builds every question and answer from a snapshot.

    Args:
      root: Snapshot directory. Defaults to the bundled snapshot.

    Returns:
      Four questions per snapshot company, in a stable order.

    Raises:
      ValueError: If the snapshot cannot support a question as specified.
    """
    root = root or bundled_snapshot()
    manifest = json.loads((root / "manifest.json").read_text())
    cutoff = manifest["recorded_on"]
    start, end = COUNT_WINDOW
    questions = []
    for position, company in enumerate(manifest["companies"]):
        ticker, cik = company["ticker"], int(company["cik"])
        name = DISPLAY_NAMES[ticker]
        rows = _rows(
            _load(root, f"data.sec.gov/submissions/CIK{cik:010d}.json")
        )
        if min(r["filingDate"] for r in rows) > start:
            raise ValueError(f"{ticker}: snapshot does not cover {start}")

        annual = max(
            (
                r
                for r in rows
                if r["form"] == "10-K" and r["filingDate"] <= cutoff
            ),
            key=lambda r: (r["filingDate"], r["accessionNumber"]),
        )
        questions.append(
            _question(
                ticker,
                name,
                Category.LATEST_ANNUAL_REPORT,
                f"What is the accession number of the most recent annual"
                f" report on Form 10-K that {name} filed on or before"
                f" {cutoff}?",
                annual["accessionNumber"],
                f"10-K filed {annual['filingDate']}",
            )
        )

        quarterly = max(
            (r for r in rows if r["form"] == "10-Q"),
            key=lambda r: (r["filingDate"], r["accessionNumber"]),
        )
        period = quarterly["reportDate"]
        if (
            sum(r["form"] == "10-Q" and r["reportDate"] == period for r in rows)
            != 1
        ):
            raise ValueError(f"{ticker}: several 10-Qs for period {period}")
        questions.append(
            _question(
                ticker,
                name,
                Category.QUARTERLY_FILING_DATE,
                f"On what date did {name} file its quarterly report on Form"
                f" 10-Q for the period ended {period}?",
                quarterly["filingDate"],
                f"10-Q {quarterly['accessionNumber']}",
            )
        )

        count = sum(
            r["form"] == "8-K" and start <= r["filingDate"] <= end for r in rows
        )
        questions.append(
            _question(
                ticker,
                name,
                Category.CURRENT_REPORT_COUNT,
                f"How many current reports on Form 8-K (not counting"
                f" amendments) did {name} file from {start} through {end}?",
                str(count),
                f"8-K filings dated {start} to {end}",
            )
        )

        rotation = METRIC_ROTATION[position:] + METRIC_ROTATION[:position]
        for key in rotation:
            found = _figure(root, cik, key, annual["accessionNumber"])
            if found is None:
                continue
            value, fiscal_year, tag = found
            questions.append(
                _question(
                    ticker,
                    name,
                    Category.ANNUAL_FIGURE,
                    f"What {METRICS[key].label} did {name} report for fiscal"
                    f" year {fiscal_year} in its annual report on Form 10-K?"
                    " Give the full amount in US dollars.",
                    str(value),
                    f"{tag} in 10-K {annual['accessionNumber']}",
                    metric=key,
                    fiscal_year=fiscal_year,
                )
            )
            break
        else:
            raise ValueError(f"{ticker}: no rotation metric is reported")
    return questions


def _question(
    ticker: str,
    name: str,
    category: Category,
    text: str,
    answer: str,
    evidence: str,
    **extra: Any,
) -> Question:
    return Question(
        task_id=f"{ticker}-{category.value}",
        category=category,
        ticker=ticker,
        company=name,
        question=text,
        answer=answer,
        kind=ANSWER_KIND[category],
        evidence=evidence,
        **extra,
    )


def dump_questions(questions: Iterable[Question], path: Path) -> None:
    """Writes questions as JSON Lines."""
    lines = [q.model_dump_json() for q in questions]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_questions(path: Path | None = None) -> list[Question]:
    """Reads questions. Defaults to the frozen dataset in the package."""
    if path is None:
        text = (
            resources.files("edgar_mcp.evals.data")
            .joinpath(DATASET)
            .read_text(encoding="utf-8")
        )
    else:
        text = path.read_text(encoding="utf-8")
    return [
        Question.model_validate_json(line) for line in text.splitlines() if line
    ]
