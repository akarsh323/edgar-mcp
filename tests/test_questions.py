"""Tests for the evaluation questions, their answers and answer grading."""

import asyncio
import json
from collections import Counter

import pytest
from mcp import Client

from edgar_mcp.config import Settings, bundled_snapshot
from edgar_mcp.evals.questions import (
    ANSWER_KIND,
    AnswerKind,
    Category,
    generate_questions,
    load_questions,
)
from edgar_mcp.evals.task import normalize_answer
from edgar_mcp.server import build_server

QUESTIONS = load_questions()


def test_frozen_dataset_matches_the_snapshot():
    assert generate_questions() == QUESTIONS


def test_shape():
    assert len(QUESTIONS) == 24
    assert len({q.task_id for q in QUESTIONS}) == 24
    assert Counter(q.category for q in QUESTIONS) == dict.fromkeys(Category, 6)
    for q in QUESTIONS:
        assert q.kind is ANSWER_KIND[q.category]
        assert normalize_answer(q.kind, q.answer) == q.answer
        assert q.company in q.question


def test_server_agrees_with_independent_ground_truth():
    """The answers come from accession numbers; the server groups by fiscal
    year. Agreement on every question cross-checks the two."""
    server = build_server(Settings(snapshot_dir=bundled_snapshot()))

    async def answer(client, q):
        async def tool(name, **arguments):
            result = await client.call_tool(name, arguments)
            assert not result.is_error, result.content[0].text
            return json.loads(result.content[0].text)

        if q.category is Category.ANNUAL_FIGURE:
            page = await tool(
                "edgar_get_financial_metric",
                company=q.company,
                metric=q.metric,
                fiscal_year=q.fiscal_year,
            )
            return str(page["values"][0]["value"])
        if q.category is Category.LATEST_ANNUAL_REPORT:
            cutoff = q.question.rsplit(" ", 1)[1].rstrip("?")
            page = await tool(
                "edgar_list_filings",
                company=q.company,
                form_types=["10-K"],
                filed_before=cutoff,
                limit=1,
            )
            return page["filings"][0]["accession_number"]
        if q.category is Category.CURRENT_REPORT_COUNT:
            page = await tool(
                "edgar_list_filings",
                company=q.company,
                form_types=["8-K"],
                filed_after="2025-01-01",
                filed_before="2025-06-30",
                limit=1,
            )
            return str(page["total_matching"])
        period = q.question.rsplit("ended ", 1)[1].rstrip("?")
        page = await tool(
            "edgar_list_filings",
            company=q.company,
            form_types=["10-Q"],
            limit=10,
        )
        return next(
            f["filing_date"]
            for f in page["filings"]
            if f["report_date"] == period
        )

    async def run():
        async with Client(server) as client:
            return [await answer(client, q) for q in QUESTIONS]

    assert asyncio.run(run()) == [q.answer for q in QUESTIONS]


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        (AnswerKind.ACCESSION, "0000320193-26-000026", "0000320193-26-000026"),
        (
            AnswerKind.ACCESSION,
            "The filing is 000032019326000026.",
            "0000320193-26-000026",
        ),
        (
            AnswerKind.ACCESSION,
            "0000320193-26-000026 or 0000320193-25-000079",
            None,
        ),
        (AnswerKind.DATE, "2026-07-31", "2026-07-31"),
        (AnswerKind.DATE, "It was filed on July 31, 2026.", "2026-07-31"),
        (AnswerKind.DATE, "31 Jul 2026", "2026-07-31"),
        (AnswerKind.DATE, "2026-07-31 or 2026-08-01", None),
        (AnswerKind.COUNT, "7", "7"),
        (AnswerKind.COUNT, "Walmart filed 7 of them.", "7"),
        (AnswerKind.COUNT, "7 or 8", None),
        (AnswerKind.AMOUNT, "29825000000", "29825000000"),
        (AnswerKind.AMOUNT, "$29,825,000,000", "29825000000"),
        (AnswerKind.AMOUNT, "$29.825 billion", "29825000000"),
        (AnswerKind.AMOUNT, "about $29.8 billion", "29800000000"),
        (AnswerKind.AMOUNT, "1,395,836,000 (2025) vs 1,234,000 (2024)", None),
        (AnswerKind.AMOUNT, "UNKNOWN", None),
        (AnswerKind.COUNT, None, None),
    ],
)
def test_normalize_answer(kind, raw, expected):
    assert normalize_answer(kind, raw) == expected
