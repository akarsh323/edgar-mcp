"""The Inspect task that measures a tool surface with an agent.

    inspect eval edgar_mcp/edgar_tools -T toolset=designed --model <model>

Each sample is one question. The agent talks to the real server, started as
a subprocess over MCP stdio and serving the bundled snapshot, so the
measurement covers the whole path a client would use: tool listing, argument
schemas, descriptions, response bodies and error messages.

Tool output is held to an explicit budget. Inspect does not truncate MCP
tool results, and real clients do (Anthropic's guidance on writing tools for
agents notes that Claude Code limits tool responses to 25,000 tokens by
default), so the budget is applied here, with the same notice Inspect uses
for other tools. It is a task parameter.
"""

from __future__ import annotations

import datetime
import decimal
import re
import sys

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ModelOutput,
    execute_tools,
    get_model,
)
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer, stderr
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import (
    Tool,
    ToolError,
    ToolSource,
    mcp_connection,
    mcp_server_stdio,
    mcp_tools,
    tool,
)
from inspect_ai.util import StoreModel, store_as

from edgar_mcp.config import BUNDLED, ENV_SNAPSHOT, ENV_TOOLSET, Toolset
from edgar_mcp.evals.questions import AnswerKind, Question, load_questions

TASK_VERSION = "1.0.0"
DEFAULT_MAX_TOOL_OUTPUT = 100_000
"""Bytes of tool output the agent sees per call: about 25,000 tokens."""

UNKNOWN = "UNKNOWN"
SUBMIT = "submit_answer"
SYSTEM_PROMPT = f"""\
You answer questions about U.S. public companies from their SEC filings. \
Use the tools to find each answer; do not answer from memory. When you have \
the answer, call `{SUBMIT}` with the value only: an accession number such as \
0000320193-25-000079, a date as YYYY-MM-DD, a whole number, or an amount as \
a whole number of US dollars. If you cannot find the answer, submit \
{UNKNOWN}."""
NUDGE = f"Continue, and call `{SUBMIT}` when you have the answer."
TRUNCATION_NOTICE = """\
The output of your call to {tool} was too long to be displayed.
Here is a truncated version:
<START_TOOL_OUTPUT>
{text}
<END_TOOL_OUTPUT>"""
_MAX_NUDGES = 2

VALUE_KEYS = (
    "correct",
    "tool_calls",
    "total_tokens",
    "tool_output_kib",
    "truncated_calls",
)


class AgentRun(StoreModel):
    """What happened in one sample, recorded by the solver.

    Attributes:
      answer: The submitted answer, or `None`.
      tool_calls: Calls to server tools (not counting the submission).
      tool_errors: Server tool calls that returned an error.
      truncated_calls: Server tool calls whose output exceeded the budget.
      tool_output_bytes: Bytes the server returned, before truncation.
      turns: Model calls.
      total_tokens: Tokens over all model calls.
      stop_reason: submitted, no_tool_call or turn_limit.
    """

    answer: str | None = None
    tool_calls: int = 0
    tool_errors: int = 0
    truncated_calls: int = 0
    tool_output_bytes: int = 0
    turns: int = 0
    total_tokens: int = 0
    stop_reason: str = "turn_limit"


@tool
def submit_answer() -> Tool:
    """Creates the tool that records the final answer."""

    async def execute(answer: str) -> str:
        """Submit the final answer. This ends the task.

        Args:
          answer: The answer value only, or UNKNOWN.

        Returns:
          Confirmation.
        """
        run = store_as(AgentRun)
        if run.answer is not None:
            raise ToolError("An answer was already submitted.")
        run.answer = answer
        return "Answer recorded."

    return execute


def edgar_tools_source(toolset: str) -> ToolSource:
    """Returns the server's tools, served over stdio from the snapshot."""
    server = mcp_server_stdio(
        name="edgar",
        command=sys.executable,
        args=["-m", "edgar_mcp"],
        env={ENV_SNAPSHOT: BUNDLED, ENV_TOOLSET: toolset},
    )
    return mcp_tools(server)


def apply_budget(
    message: ChatMessageTool, budget: int
) -> tuple[ChatMessageTool, int, bool]:
    """Truncates a tool result to `budget` bytes.

    Returns:
      The message to show the model, the original size in bytes, and
      whether it was truncated.
    """
    raw = message.text.encode("utf-8")
    if len(raw) <= budget:
        return message, len(raw), False
    clipped = raw[:budget].decode("utf-8", errors="ignore")
    shown = TRUNCATION_NOTICE.format(tool=message.function, text=clipped)
    return message.model_copy(update={"content": shown}), len(raw), True


@solver
def answer_with_edgar(
    toolset: str = Toolset.DESIGNED.value,
    max_tool_output: int = DEFAULT_MAX_TOOL_OUTPUT,
    max_turns: int = 12,
) -> Solver:
    """Runs a tool-use loop against the server until an answer is submitted.

    Args:
      toolset: Which tool surface the server exposes.
      max_tool_output: Bytes of each tool result the model may see.
      max_turns: Maximum model calls.

    Returns:
      The solver.
    """
    surface = Toolset(toolset)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        model = get_model()
        run = store_as(AgentRun)
        tools: list[Tool | ToolSource] = [
            edgar_tools_source(surface.value),
            submit_answer(),
        ]
        messages: list[ChatMessage] = [
            ChatMessageSystem(content=SYSTEM_PROMPT),
            ChatMessageUser(content=state.input_text),
        ]
        nudges = 0
        async with mcp_connection(tools):
            while run.turns < max_turns:
                output = await model.generate(messages, tools)
                run.turns += 1
                run.total_tokens += (
                    output.usage.total_tokens if output.usage else 0
                )
                messages.append(output.message)
                if not output.message.tool_calls:
                    if nudges < _MAX_NUDGES:
                        nudges += 1
                        messages.append(ChatMessageUser(content=NUDGE))
                        continue
                    run.stop_reason = "no_tool_call"
                    break
                result = await execute_tools(messages, tools)
                for message in result.messages:
                    if isinstance(message, ChatMessageTool) and (
                        message.function != SUBMIT
                    ):
                        run.tool_calls += 1
                        run.tool_errors += message.error is not None
                        message, size, cut = apply_budget(
                            message, max_tool_output
                        )
                        run.tool_output_bytes += size
                        run.truncated_calls += cut
                    messages.append(message)
                if run.answer is not None:
                    run.stop_reason = "submitted"
                    break

        state.messages = messages
        state.output = ModelOutput.from_content(
            model=str(model), content=run.answer or ""
        )
        return state

    return solve


_MONTHS = "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"
_AMOUNT = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(thousand|million|billion|trillion)?\b",
    re.IGNORECASE,
)
_SCALE = {
    "thousand": 10**3,
    "million": 10**6,
    "billion": 10**9,
    "trillion": 10**12,
}


def normalize_answer(kind: AnswerKind, raw: str | None) -> str | None:
    """Puts an answer in canonical form, or returns `None` if it has none.

    Answers may carry extra words ("The 10-K was filed on 2026-07-31."), but
    must contain exactly one candidate value, so hedging between two values
    is never rewarded.
    """
    if raw is None or raw.strip().upper() in ("", UNKNOWN):
        return None
    text = raw.strip()
    match kind:
        case AnswerKind.ACCESSION:
            found = set(re.findall(r"\b\d{10}-?\d{2}-?\d{6}\b", text))
            if len(found) != 1:
                return None
            digits = re.sub(r"\D", "", found.pop())
            return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
        case AnswerKind.DATE:
            dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text))
            cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b|,", "", text)
            for fmt in _MONTHS:
                for words in re.findall(
                    r"[A-Za-z]+\.? \d{1,2} \d{4}|\d{1,2} [A-Za-z]+ \d{4}",
                    cleaned,
                ):
                    try:
                        parsed = datetime.datetime.strptime(
                            words.replace(".", ""), fmt
                        )
                    except ValueError:
                        continue
                    dates.add(parsed.date().isoformat())
            return dates.pop() if len(dates) == 1 else None
        case AnswerKind.COUNT:
            numbers = set(re.findall(r"(?<![\d-])\d+(?![\d-])", text))
            return str(int(numbers.pop())) if len(numbers) == 1 else None
        case AnswerKind.AMOUNT:
            amounts = set()
            for number, scale in _AMOUNT.findall(text.replace("$", " ")):
                value = decimal.Decimal(number.replace(",", ""))
                value *= _SCALE.get(scale.lower(), 1)
                if value == value.to_integral_value():
                    amounts.add(int(value))
            return str(amounts.pop()) if len(amounts) == 1 else None
    raise AssertionError(f"unhandled answer kind: {kind}")


@scorer(metrics={key: [mean(), stderr()] for key in VALUE_KEYS})
def edgar_scorer() -> Scorer:
    """Scores the submitted answer by exact match after normalization."""

    async def score(state: TaskState, target: Target) -> Score:
        question = Question.model_validate(state.metadata["question"])
        run = state.store_as(AgentRun)
        got = normalize_answer(question.kind, run.answer)
        correct = got is not None and got == question.answer
        return Score(
            value={
                "correct": float(correct),
                "tool_calls": float(run.tool_calls),
                "total_tokens": float(run.total_tokens),
                "tool_output_kib": run.tool_output_bytes / 1024,
                "truncated_calls": float(run.truncated_calls),
            },
            answer=run.answer,
            explanation=f"expected {question.answer}, read {got}",
            metadata={
                "category": question.category.value,
                "correct": correct,
                **run.model_dump(exclude={"answer"}),
            },
        )

    return score


@task
def edgar_tools(
    toolset: str = Toolset.DESIGNED.value,
    max_tool_output: int = DEFAULT_MAX_TOOL_OUTPUT,
    max_turns: int = 12,
) -> Task:
    """Questions about SEC filings, answered through the edgar MCP server.

    Args:
      toolset: `designed` or `baseline`.
      max_tool_output: Bytes of each tool result the model may see.
      max_turns: Maximum model calls per question.

    Returns:
      The task.
    """
    surface = Toolset(toolset)
    questions = load_questions()
    return Task(
        dataset=MemoryDataset(
            [
                Sample(
                    id=q.task_id,
                    input=q.question,
                    target=q.answer,
                    metadata={"question": q.model_dump(mode="json")},
                )
                for q in questions
            ],
            name="edgar-questions-v1",
        ),
        solver=answer_with_edgar(
            toolset=surface.value,
            max_tool_output=max_tool_output,
            max_turns=max_turns,
        ),
        scorer=edgar_scorer(),
        version=TASK_VERSION,
        metadata={"toolset": surface.value, "max_tool_output": max_tool_output},
    )
