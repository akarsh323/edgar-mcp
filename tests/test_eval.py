"""End-to-end tests: Inspect, the scripted agent and the server over stdio.

Each sample starts the server as a subprocess and talks MCP to it, exactly
as a desktop client would, so these tests cover the full path.
"""

import json

import pytest
from inspect_ai import eval as inspect_eval
from inspect_ai.model import ChatMessageTool

from edgar_mcp.evals.cli import main
from edgar_mcp.evals.task import (
    DEFAULT_MAX_TOOL_OUTPUT,
    apply_budget,
    edgar_tools,
)

pytestmark = pytest.mark.e2e
INSPECT_DEFAULT_BUDGET = 16 * 1024


def scores(log):
    assert log.status == "success"
    assert all(sample.error is None for sample in log.samples)
    return [sample.scores["edgar_scorer"].value for sample in log.samples]


@pytest.fixture(scope="module")
def log_dir(tmp_path_factory):
    directory = tmp_path_factory.mktemp("logs")
    code = main(
        [
            "run",
            "--model",
            "edgar_mcp/scripted",
            "--toolset",
            "both",
            "--max-tool-output",
            str(DEFAULT_MAX_TOOL_OUTPUT),
            "--max-tool-output",
            str(INSPECT_DEFAULT_BUDGET),
            "--log-dir",
            str(directory),
            "--display",
            "none",
        ]
    )
    assert code == 0
    return directory


@pytest.fixture(scope="module")
def results(log_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("results")
    assert (
        main(["report", str(log_dir), "--out", str(out), "--resamples", "200"])
        == 0
    )
    payload = json.loads((out / "results.json").read_text())
    return {(g["toolset"], g["max_tool_output"]): g for g in payload["groups"]}


def test_designed_tools_answer_everything_in_one_call(results):
    for budget in (DEFAULT_MAX_TOOL_OUTPUT, INSPECT_DEFAULT_BUDGET):
        group = results[("designed", budget)]
        assert group["accuracy"]["value"] == 1.0
        assert group["tool_calls_per_question"] == 1.0
        assert group["truncated_share_of_calls"] == 0.0
        assert group["tool_output_kib_per_question"] < 2


def test_raw_tools_reach_the_data_only_at_a_large_budget(results):
    large = results[("baseline", DEFAULT_MAX_TOOL_OUTPUT)]
    small = results[("baseline", INSPECT_DEFAULT_BUDGET)]
    designed = results[("designed", DEFAULT_MAX_TOOL_OUTPUT)]
    # An ideal reader still finds every answer within 100,000 bytes...
    assert large["accuracy"]["value"] == 1.0
    # ...but at a cost of two orders of magnitude more context.
    assert large["tokens_per_question"] > 100 * designed["tokens_per_question"]
    assert large["truncated_share_of_calls"] > 0.8
    # At Inspect's default budget for other tools, nothing it needs fits.
    assert small["accuracy"]["value"] == 0.0
    assert small["truncated_share_of_calls"] == 1.0
    assert all(g["excluded"] == 0 for g in results.values())


def test_report_markdown(log_dir, tmp_path):
    assert (
        main(
            [
                "report",
                str(log_dir),
                "--out",
                str(tmp_path),
                "--resamples",
                "100",
            ]
        )
        == 0
    )
    markdown = (tmp_path / "results.md").read_text()
    assert "edgar_mcp/scripted (scripted, not an LLM)" in markdown
    assert "100,000 B" in markdown
    assert main(["report", str(tmp_path / "missing")]) == 1


def test_single_task_through_inspect_directly(tmp_path):
    (log,) = inspect_eval(
        edgar_tools(toolset="designed"),
        model="edgar_mcp/scripted",
        limit=2,
        log_dir=str(tmp_path),
        display="none",
    )
    assert [v["correct"] for v in scores(log)] == [1.0, 1.0]


def test_apply_budget():
    message = ChatMessageTool(content="x" * 50, tool_call_id="1", function="t")
    same, size, cut = apply_budget(message, 100)
    assert (same is message, size, cut) == (True, 50, False)
    shown, size, cut = apply_budget(message, 10)
    assert (size, cut) == (50, True)
    assert "x" * 10 + "\n<END_TOOL_OUTPUT>" in shown.text
    assert "too long to be displayed" in shown.text


def test_unknown_toolset_fails_fast():
    with pytest.raises(ValueError, match="not a valid Toolset"):
        edgar_tools(toolset="fancy")
