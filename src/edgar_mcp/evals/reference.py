"""A scripted reference agent, packaged as an Inspect model provider.

    inspect eval edgar_mcp/edgar_tools --model edgar_mcp/scripted

This is not a language model. It recognizes the question templates and
plays a fixed strategy for whichever tool surface it is given:

  * designed: one filtered call to the right tool, then submit.
  * baseline: the raw endpoints, read the way an ideal reader would read
    them: it finds the company in the tickers file, aligns the columnar
    arrays of the submissions document by index, and applies the correct
    fiscal-year rule, all from whatever part of each response it was shown.
    When the part it needs was cut off by the output budget, it answers
    UNKNOWN rather than guess.

The scripted reader is better at raw JSON than any model, so its baseline
accuracy is an upper bound. Its token counts are character-based estimates.
What it does measure exactly is the cost and reach of each surface: how many
calls, how many bytes, and whether the data needed fits in what the agent
is shown.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    ModelAPI,
    ModelOutput,
    ModelUsage,
    modelapi,
)
from inspect_ai.tool import ToolChoice, ToolInfo

from edgar_mcp.edgar import METRICS, core_name, select_values
from edgar_mcp.evals.task import SUBMIT, UNKNOWN

PROVIDER = "edgar_mcp"
_CHARS_PER_TOKEN = 4
_LABEL_TO_METRIC = {spec.label: key for key, spec in METRICS.items()}

_PATTERNS = {
    "latest_annual_report": re.compile(
        r"most recent annual report on Form 10-K that (?P<company>.+?) filed"
        r" on or before (?P<cutoff>\d{4}-\d{2}-\d{2})\?"
    ),
    "quarterly_filing_date": re.compile(
        r"did (?P<company>.+?) file its quarterly report on Form 10-Q for the"
        r" period ended (?P<period>\d{4}-\d{2}-\d{2})\?"
    ),
    "current_report_count": re.compile(
        r"did (?P<company>.+?) file from (?P<start>\d{4}-\d{2}-\d{2}) through"
        r" (?P<end>\d{4}-\d{2}-\d{2})\?"
    ),
    "annual_figure": re.compile(
        r"What (?P<label>.+?) did (?P<company>.+?) report for fiscal year"
        r" (?P<year>\d{4}) in its annual report"
    ),
}
_TICKER_ENTRY = re.compile(
    r'\{"cik_str":(\d+),"ticker":"([^"]*)","title":"((?:[^"\\]|\\.)*)"\}'
)
_SHOWN = re.compile(r"<START_TOOL_OUTPUT>\n(.*)\n<END_TOOL_OUTPUT>", re.DOTALL)


@dataclasses.dataclass(frozen=True)
class Action:
    """A tool call, or a final text reply when `tool` is `None`."""

    tool: str | None
    arguments: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _Exchange:
    tool: str
    arguments: dict[str, Any]
    text: str
    failed: bool
    truncated: bool


def _exchanges(messages: Sequence[ChatMessage]) -> list[_Exchange]:
    replies = {
        m.tool_call_id: m for m in messages if isinstance(m, ChatMessageTool)
    }
    found = []
    for message in messages:
        if not isinstance(message, ChatMessageAssistant):
            continue
        for call in message.tool_calls or []:
            reply = replies.get(call.id)
            if reply is None:
                continue
            text = reply.text
            shown = _SHOWN.search(text)
            found.append(
                _Exchange(
                    tool=call.function,
                    arguments=call.arguments,
                    text=shown.group(1) if shown else text,
                    failed=reply.error is not None,
                    truncated=shown is not None,
                )
            )
    return found


def _submit(answer: str) -> Action:
    return Action(SUBMIT, {"answer": answer})


def next_action(
    messages: Sequence[ChatMessage], tools: Iterable[str]
) -> Action:
    """Decides the scripted agent's next step from the conversation so far.

    Args:
      messages: The conversation.
      tools: Names of the available tools.

    Returns:
      The next tool call, or a text reply if the question is not one of the
      benchmark's.
    """
    available = set(tools)
    question = next(m.text for m in messages if isinstance(m, ChatMessageUser))
    found = next(
        (
            (category, match)
            for category, pattern in _PATTERNS.items()
            if (match := pattern.search(question))
        ),
        None,
    )
    if found is None:
        return Action(None)
    category, match = found
    fields = match.groupdict()
    history = _exchanges(messages)
    if "edgar_list_filings" in available:
        return _designed(category, fields, history)
    return _baseline(category, fields, history)


def _designed(
    category: str, fields: dict[str, str], history: list[_Exchange]
) -> Action:
    company = fields["company"]
    if category == "annual_figure":
        metric_call = {
            "company": company,
            "metric": _LABEL_TO_METRIC[fields["label"]],
            "fiscal_year": int(fields["year"]),
        }
        if not history:
            return Action("edgar_get_financial_metric", metric_call)
        last = history[-1]
        if last.failed:
            return _submit(UNKNOWN)
        return _submit(str(json.loads(last.text)["values"][0]["value"]))

    forms = {
        "latest_annual_report": ["10-K"],
        "quarterly_filing_date": ["10-Q"],
        "current_report_count": ["8-K"],
    }[category]
    call: dict[str, Any] = {"company": company, "form_types": forms}
    if category == "latest_annual_report":
        call |= {"filed_before": fields["cutoff"], "limit": 1}
    elif category == "current_report_count":
        call |= {
            "filed_after": fields["start"],
            "filed_before": fields["end"],
            "limit": 1,
        }
    if not history:
        return Action("edgar_list_filings", call)
    last = history[-1]
    if last.failed:
        return _submit(UNKNOWN)
    page = json.loads(last.text)
    if category == "latest_annual_report":
        return _submit(page["filings"][0]["accession_number"])
    if category == "current_report_count":
        return _submit(str(page["total_matching"]))
    for filing in page["filings"]:
        if filing["report_date"] == fields["period"]:
            return _submit(filing["filing_date"])
    if page["next_offset"] is None:
        return _submit(UNKNOWN)
    return Action("edgar_list_filings", call | {"offset": page["next_offset"]})


def _complete_array(text: str, name: str) -> list[Any] | None:
    found = re.search(rf'"{name}":\[(.*?)\]', text, re.DOTALL)
    return json.loads(f"[{found.group(1)}]") if found else None


def _baseline(
    category: str, fields: dict[str, str], history: list[_Exchange]
) -> Action:
    if not history:
        return Action("get_company_tickers")
    wanted = core_name(fields["company"])
    cik = None
    for entry in _TICKER_ENTRY.finditer(history[0].text):
        title = core_name(json.loads(f'"{entry.group(3)}"'))
        if title == wanted or re.match(rf"^{re.escape(wanted)}\b", title):
            cik = int(entry.group(1))
            break
    if cik is None:
        return _submit(UNKNOWN)
    padded = f"{cik:010d}"

    if category == "annual_figure":
        return _baseline_figure(fields, history[1:], padded)

    if len(history) == 1:
        return Action("get_submissions", {"cik": padded})
    document = history[1]
    if document.failed:
        return _submit(UNKNOWN)
    columns = {
        name: _complete_array(document.text, name)
        for name in ("accessionNumber", "filingDate", "reportDate", "form")
    }
    if any(values is None for values in columns.values()):
        return _submit(UNKNOWN)
    rows = [
        {name: values[i] for name, values in columns.items() if values}
        for i in range(len(columns["accessionNumber"] or []))
    ]
    if category == "latest_annual_report":
        annual = [
            r
            for r in rows
            if r["form"] == "10-K" and r["filingDate"] <= fields["cutoff"]
        ]
        if not annual:
            return _submit(UNKNOWN)
        best = max(
            annual, key=lambda r: (r["filingDate"], r["accessionNumber"])
        )
        return _submit(best["accessionNumber"])
    if category == "current_report_count":
        count = sum(
            r["form"] == "8-K"
            and fields["start"] <= r["filingDate"] <= fields["end"]
            for r in rows
        )
        return _submit(str(count))
    for r in rows:
        if r["form"] == "10-Q" and r["reportDate"] == fields["period"]:
            return _submit(r["filingDate"])
    return _submit(UNKNOWN)


def _baseline_figure(
    fields: dict[str, str], concept_calls: list[_Exchange], cik: str
) -> Action:
    spec = METRICS[_LABEL_TO_METRIC[fields["label"]]]
    year = int(fields["year"])
    for tag, exchange in zip(spec.tags, concept_calls, strict=False):
        if exchange.failed or exchange.truncated:
            continue
        values = select_values([(tag, json.loads(exchange.text))], spec, "FY")
        for value in values:
            if value.fiscal_year == year:
                return _submit(str(value.value))
    if len(concept_calls) < len(spec.tags):
        tag = spec.tags[len(concept_calls)]
        return Action(
            "get_company_concept",
            {"cik": cik, "taxonomy": spec.taxonomy, "tag": tag},
        )
    return _submit(UNKNOWN)


@modelapi(name=PROVIDER)
class ScriptedAgentAPI(ModelAPI):
    """Inspect model provider for the scripted agent: `edgar_mcp/scripted`."""

    def __init__(
        self,
        model_name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        config: GenerateConfig = GenerateConfig(),  # noqa: B008
        **model_args: Any,
    ) -> None:
        """Creates the provider.

        Args:
          model_name: Must be `scripted`.
          base_url: Unused; accepted for interface compatibility.
          api_key: Unused; accepted for interface compatibility.
          config: Unused; accepted for interface compatibility.
          **model_args: Unused; accepted for interface compatibility.

        Raises:
          ValueError: For any other model name.
        """
        super().__init__(model_name, base_url, api_key, [], config)
        if model_name.split("/")[-1] != "scripted":
            raise ValueError(f"unknown scripted agent: {model_name}")

    async def generate(
        self,
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        """Returns the agent's next step as a model output."""
        action = next_action(input, [t.name for t in tools])
        model = f"{PROVIDER}/scripted"
        if action.tool is None:
            output = ModelOutput.from_content(model=model, content="Done.")
            produced = "Done."
        else:
            output = ModelOutput.for_tool_call(
                model=model,
                tool_name=action.tool,
                tool_arguments=action.arguments,
            )
            produced = action.tool + json.dumps(action.arguments)
        # Character-based estimate, so that token accounting is exercised.
        # Not comparable to a real tokenizer.
        input_tokens = sum(len(m.text) for m in input) // _CHARS_PER_TOKEN
        output_tokens = max(1, len(produced) // _CHARS_PER_TOKEN)
        output.usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )
        return output
