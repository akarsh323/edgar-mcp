# Design

Status: implemented (v0.1.0). Last updated 2026-11-18.

## Problem

An agent answering questions about public companies needs three things from
EDGAR: who a company is, what it filed, and what it reported. The SEC's APIs
provide all three, shaped for programs: a 777 KiB ticker file, a filing
index whose recent block is a dozen parallel arrays of about 1,000 entries
each, and XBRL concept documents in which every value is tagged with the
fiscal year of the filing that reported it rather than of the period it
covers.

A wrapper that passes those documents through is easy to write and costly to
use. This project asks how much a task-shaped tool surface is worth, and
answers with a measurement rather than an opinion.

## Goals

1. A correct, read-only MCP server for the three questions above, usable
   live and offline.
2. Respect for EDGAR's fair-access rules by construction.
3. An evaluation that compares tool surfaces on identical questions, data
   and agent, through the real MCP path.
4. Ground truth that does not depend on the code under test.

## Non-goals

- Full-text search, filing documents, insider or ownership data.
- IFRS filers and figures beyond the eight supported.
- A ranking of models.

## The server

### Tool surface

Four tools, each answering a question a person would ask, under one
`edgar_` prefix. Fewer, broader tools mean fewer choices for the model and
fewer round trips; the prefix keeps them apart from other servers' tools in
the same client.

| Choice | Reason |
| --- | --- |
| `company` accepts ticker, name or CIK | Models know tickers and names. Zero-padded CIKs are an implementation detail. |
| Ambiguous names are errors listing the candidates | Auto-picking "Apple" could return Apple Hospitality REIT. A wrong company produces confident, wrong figures. |
| `form_types`, date bounds, `limit`, `offset` | The agent asks for what it needs; responses stay small. |
| `total_matching` in every page | Counting questions need one call, not pagination. |
| `response_format: concise \| detailed` | Most questions need four fields per filing; links and 8-K items are opt-in. |
| Compact JSON text, not structured output | The SDK renders structured output as indented JSON. Compact text is 22-28% smaller on these tools, and every client can read it. |
| Errors name the problem and the fix | "Did you mean", the valid range of fiscal years, the date format expected. |
| Read-only annotations | Clients can skip confirmation prompts for tools that cannot change anything. |

### The fiscal-year rule

`edgar_get_financial_metric` returns, for each filing of the requested kind
(10-K for `FY`, 10-Q for `Q1`-`Q3`), the value whose period ends last and
whose length matches the period: about a year for `FY`, about a quarter for
`Q1`-`Q3` (so a 10-Q's three-month figure, not its year-to-date one), and a
single date for balance-sheet figures. When a figure moved between XBRL tags
over the years (revenue moved to ASC 606 tags in 2018), each year takes the
most preferred tag that reports it. Within one tag, the first filing for a
fiscal year wins, so a later restatement does not replace the figure as
first reported.

### Fair access

The SEC asks automated clients to declare who they are and to stay under 10
requests per second. Live requests without `EDGAR_MCP_USER_AGENT` fail
before any network call, with a message that says how to fix it. Requests
are spaced at 5 per second by default; 429 and 5xx responses are tried up
to three times in all, with exponential backoff that honours `Retry-After`; a 403,
which the SEC returns to undeclared clients, is reported as a configuration
problem rather than retried. Responses are cached in memory for ten
minutes.

### Offline mode

`SnapshotTransport` is an `httpx` transport that serves files laid out like
the SEC's URLs. Everything above it runs unchanged, so the tests and the
evaluation exercise the same code as live use.

## The evaluation

### Questions and ground truth

Four kinds of question per company: the latest 10-K's accession number, a
10-Q's filing date, a count of 8-Ks in a fixed window, and one annual figure.
Each needs a lookup: accession numbers and dates are not memorable, and the
figures come from reports filed in 2026 or late 2025.

Answers are computed by `questions.py` directly from the raw snapshot files,
by a different route from the server. For the annual figure, it finds the
values carrying the latest 10-K's accession number and takes the one with
the latest period end of the right length. The server groups by the filing's
fiscal-year label instead. A test checks that the two agree on every
question.

### Harness

Each sample starts `python -m edgar_mcp` as a subprocess serving the bundled
snapshot and connects with Inspect's MCP stdio client. A simple agent loop
runs until the model calls `submit_answer`. Answers are normalized by kind
(accession digits, ISO dates, a single integer, a dollar amount with
magnitude words) and must contain exactly one candidate value.

### Output budget

Inspect truncates tool output only when a tool returns a string; MCP results
arrive as content lists and pass through at any size. Real clients do cap
them, so the harness applies an explicit per-call budget with the notice
Inspect uses elsewhere. The default, 100,000 bytes, is about the 25,000
tokens Claude Code allows a tool response. The reference results also report
16,384 bytes, Inspect's default for other tools, to show how sensitive each
surface is to the limit.

### The scripted agent

It recognizes the four question templates and plays a fixed strategy for
whichever surface it is given. On the raw surface it is an idealized reader:
it finds the company in the visible part of the ticker file, aligns the
filing index's arrays by position, and applies the correct fiscal-year rule,
answering `UNKNOWN` when the part it needs was cut off. A model would do no
better at that, so the scripted baseline accuracy is an upper bound, and the
cost figures are what a model would pay at least.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| One tool per SEC endpoint | That is the baseline. It costs two orders of magnitude more context per answer. |
| Fuzzy-match names and pick the best | Wrong companies produce plausible wrong numbers. An explicit list costs one extra call. |
| Return `frame`-aligned calendar values | Calendar frames do not match fiscal years for companies like Apple, Microsoft or Walmart, and questions are asked in fiscal years. |
| Structured output with an output schema | Larger responses, and not every client uses the schema. Worth revisiting when clients do. |
| An LLM judge for answers | Every answer here is an exact value. |
| Evaluate against live EDGAR | Answers change with every filing; results would not be reproducible. The snapshot pins them. |
| Mock SEC responses by hand | Real responses carry the quirks (tag changes, restatements, 52-53 week years) that the server has to handle. |

## Known weaknesses

- Only the recent-filings block of the index is read.
- Six companies, all large US filers with clean XBRL.
- The scripted agent measures cost and reach, not comprehension.
- `difflib` suggestions are a heuristic; unusual names may get none.

## Testing

- Client: fair-access rules, retries, `Retry-After`, caching, snapshot
  serving, with fake clocks and transports and no network.
- Domain: resolution, filters, and the fiscal-year rule on synthetic
  concept data built to trip the naive rule.
- Protocol: a real MCP client against the server in process, for both
  surfaces, including every error path.
- Ground truth: the frozen questions regenerate from the snapshot, and the
  server agrees with all 24 answers.
- End to end: Inspect, the scripted agent and the server over stdio, for
  both surfaces at two budgets.
- CI on Python 3.11 to 3.13 with a 90% coverage floor.
