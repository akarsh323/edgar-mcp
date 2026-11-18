"""Command-line entry point for the evaluation.

    edgar-mcp-eval run --model anthropic/claude-sonnet-5-5 --epochs 3
    edgar-mcp-eval report logs/ --out results/models

Results are grouped by model, tool surface and output budget. Accuracy
intervals are bootstraps over questions, with each question's epochs kept
together. Samples that ended in an error are excluded and counted.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import importlib.metadata
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from edgar_mcp import __version__
from edgar_mcp.config import Toolset
from edgar_mcp.evals.task import DEFAULT_MAX_TOOL_OUTPUT

SCORER = "edgar_scorer"
SCRIPTED_PREFIX = "edgar_mcp/"


@dataclasses.dataclass(frozen=True)
class Group:
    """One row of the results table."""

    model: str
    toolset: str
    max_tool_output: int


@dataclasses.dataclass(frozen=True)
class Row:
    """One scored sample."""

    group: Group
    question: str
    category: str
    value: dict[str, float]


def load(log_dir: Path) -> tuple[list[Row], Counter[tuple[Group, str]]]:
    """Reads scored samples and counts excluded ones per group and reason."""
    from inspect_ai.log import list_eval_logs, read_eval_log

    rows: list[Row] = []
    excluded: Counter[tuple[Group, str]] = Counter()
    for info in list_eval_logs(str(log_dir)):
        log = read_eval_log(
            info, exclude_fields={"messages", "events", "store"}
        )
        if log.eval.task.split("/")[-1] != "edgar_tools":
            continue
        args = log.eval.task_args or {}
        group = Group(
            log.eval.model,
            str(args.get("toolset", Toolset.DESIGNED.value)),
            int(args.get("max_tool_output", DEFAULT_MAX_TOOL_OUTPUT)),
        )
        for sample in log.samples or []:
            score = (sample.scores or {}).get(SCORER)
            if sample.error is not None or score is None:
                reason = (
                    sample.error.message.strip().splitlines()[-1]
                    if sample.error
                    else "not scored"
                )
                excluded[(group, reason)] += 1
                continue
            rows.append(
                Row(
                    group,
                    str(sample.id),
                    str((score.metadata or {}).get("category", "")),
                    {k: float(v) for k, v in dict(score.value).items()},  # type: ignore[arg-type]
                )
            )
    return rows, excluded


def bootstrap(
    clusters: Sequence[tuple[float, float]], resamples: int, seed: int
) -> tuple[float, float, float]:
    """Pooled rate and a 95% percentile interval over clusters."""
    counts = np.asarray(clusters, dtype=float).reshape(-1, 2)
    total = float(counts[:, 1].sum())
    if total == 0:
        return math.nan, math.nan, math.nan
    rate = float(counts[:, 0].sum()) / total
    if len(counts) < 2:
        return rate, math.nan, math.nan
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(counts), size=(resamples, len(counts)))
    sums = counts[picks].sum(axis=1)
    rates = sums[:, 0] / sums[:, 1]
    low, high = np.quantile(rates, [0.025, 0.975])
    return rate, float(low), float(high)


def summarize(
    rows: Sequence[Row],
    excluded: Counter[tuple[Group, str]],
    resamples: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Computes every reported figure for each group."""
    by_group: dict[Group, list[Row]] = defaultdict(list)
    for row in rows:
        by_group[row.group].append(row)
    groups = sorted(
        set(by_group) | {g for g, _ in excluded},
        key=lambda g: (g.model, g.toolset != "designed", -g.max_tool_output),
    )
    summary = []
    for group in groups:
        items = by_group.get(group, [])
        per_question: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        per_category: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for row in items:
            for table, key in (
                (per_question, row.question),
                (per_category, row.category),
            ):
                table[key][0] += row.value["correct"]
                table[key][1] += 1
        accuracy = bootstrap(
            [(a, b) for a, b in per_question.values()], resamples, seed
        )
        n = len(items) or 1
        calls = sum(r.value["tool_calls"] for r in items)
        summary.append(
            {
                **dataclasses.asdict(group),
                "samples": len(items),
                "questions": len(per_question),
                "excluded": sum(
                    c for (g, _), c in excluded.items() if g == group
                ),
                "accuracy": dict(
                    zip(("value", "low", "high"), accuracy, strict=True)
                ),
                "accuracy_by_category": {
                    k: v[0] / v[1] for k, v in sorted(per_category.items())
                },
                "tool_calls_per_question": calls / n,
                "tokens_per_question": sum(
                    r.value["total_tokens"] for r in items
                )
                / n,
                "tool_output_kib_per_question": sum(
                    r.value["tool_output_kib"] for r in items
                )
                / n,
                "truncated_share_of_calls": (
                    sum(r.value["truncated_calls"] for r in items) / calls
                    if calls
                    else 0.0
                ),
            }
        )
    return summary


def _pct(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value * 100:.1f}%"


def render(summary: Sequence[dict[str, Any]]) -> str:
    """Renders the summary as Markdown."""
    header = (
        "| Model | Tool surface | Output budget | Questions | Accuracy | "
        "Tool calls / question | Tokens / question | Tool output / question | "
        "Calls over budget | Excluded |\n"
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    lines = [header]
    for s in summary:
        model = s["model"]
        if model.startswith(SCRIPTED_PREFIX):
            model = f"{model} (scripted, not an LLM)"
        acc = s["accuracy"]
        interval = (
            ""
            if math.isnan(acc["low"])
            else f" [{acc['low'] * 100:.1f}, {acc['high'] * 100:.1f}]"
        )
        lines.append(
            f"| {model} | `{s['toolset']}` | {s['max_tool_output']:,} B"
            f" | {s['questions']} | {_pct(acc['value'])}{interval}"
            f" | {s['tool_calls_per_question']:.2f}"
            f" | {s['tokens_per_question']:,.0f}"
            f" | {s['tool_output_kib_per_question']:,.1f} KiB"
            f" | {_pct(s['truncated_share_of_calls'])} | {s['excluded']} |"
        )
    categories = sorted({c for s in summary for c in s["accuracy_by_category"]})
    lines += [
        "",
        "Accuracy by question type:",
        "",
        "| Model | Tool surface | Output budget | "
        + " | ".join(f"`{c}`" for c in categories)
        + " |",
        "| --- | --- | ---: |" + " ---: |" * len(categories),
    ]
    for s in summary:
        cells = " | ".join(
            _pct(s["accuracy_by_category"].get(c, math.nan)) for c in categories
        )
        lines.append(
            f"| {s['model']} | `{s['toolset']}` |"
            f" {s['max_tool_output']:,} B | {cells} |"
        )
    return "\n".join(lines) + "\n"


def _run(args: argparse.Namespace) -> int:
    from inspect_ai import eval as inspect_eval

    from edgar_mcp.evals.task import edgar_tools

    toolsets = (
        [t.value for t in Toolset] if args.toolset == "both" else [args.toolset]
    )
    tasks = [
        edgar_tools(toolset=toolset, max_tool_output=budget)
        for toolset in toolsets
        for budget in args.max_tool_output or [DEFAULT_MAX_TOOL_OUTPUT]
    ]
    logs = inspect_eval(
        tasks,
        model=args.model,
        epochs=args.epochs,
        log_dir=args.log_dir,
        max_connections=args.max_connections,
        fail_on_error=False,
        display=args.display,
    )
    failed = [log for log in logs if log.status != "success"]
    for log in failed:
        print(f"{log.eval.task} on {log.eval.model}: {log.status}")
    return 1 if failed else 0


def _report(args: argparse.Namespace) -> int:
    rows, excluded = load(args.log_dir)
    if not rows and not excluded:
        print(f"No edgar_tools samples under {args.log_dir}.")
        return 1
    summary = summarize(rows, excluded, args.resamples, args.seed)
    markdown = render(summary)
    if args.out is None:
        print(markdown)
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.md").write_text(markdown, encoding="utf-8")
    payload = {
        "generated_on": datetime.date.today().isoformat(),
        "edgar_mcp_version": __version__,
        "inspect_ai_version": importlib.metadata.version("inspect-ai"),
        "resamples": args.resamples,
        "seed": args.seed,
        "groups": summary,
        "excluded": [
            {**dataclasses.asdict(g), "reason": reason, "count": count}
            for (g, reason), count in sorted(
                excluded.items(), key=lambda kv: (kv[0][0].model, kv[0][1])
            )
        ],
    }
    (args.out / "results.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {args.out / 'results.md'} and {args.out / 'results.json'}.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the evaluation command-line interface.

    Args:
      argv: Arguments, without the program name. Defaults to `sys.argv`.

    Returns:
      Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="edgar-mcp-eval",
        description="Measure the edgar MCP tool surfaces with an agent.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run the evaluation.")
    run.add_argument("--model", action="append", required=True)
    run.add_argument(
        "--toolset",
        choices=["both", *(t.value for t in Toolset)],
        default="both",
    )
    run.add_argument(
        "--max-tool-output",
        type=int,
        action="append",
        help="Bytes of tool output shown per call. Repeat for several.",
    )
    run.add_argument("--epochs", type=int, default=1)
    run.add_argument("--log-dir", default="logs")
    run.add_argument("--max-connections", type=int)
    run.add_argument("--display", default="full")

    report = commands.add_parser("report", help="Tabulate eval logs.")
    report.add_argument("log_dir", type=Path)
    report.add_argument("--out", type=Path)
    report.add_argument("--resamples", type=int, default=10_000)
    report.add_argument("--seed", type=int, default=0)

    args = parser.parse_args(argv)
    handlers = {"run": _run, "report": _report}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
