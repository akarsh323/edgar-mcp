# edgar-mcp

[![CI](https://github.com/akarsh323/edgar-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/akarsh323/edgar-mcp/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-informational)](https://modelcontextprotocol.io)

**An [MCP](https://modelcontextprotocol.io) server for SEC EDGAR filings and
XBRL financial data, plus an evaluation that measures whether its tool design
actually helps an agent.**

Most tool servers for agents mirror an API one endpoint at a time. This repo
builds the task-shaped version, keeps a thin endpoint wrapper next to it, and
runs the same questions through both with the same agent, so each design
choice is backed by a number rather than a preference.

> **Status.** The server works live against the SEC and offline from a
> bundled snapshot. The evaluation has been run end to end with a scripted
> reference agent (see [Evaluation](#evaluation)). **No language model has
> been run yet**, because that needs API credits; the command is below.

## Tools

| Tool | Answers | Main parameters |
| --- | --- | --- |
| `edgar_find_company` | Which company is this? | `query` (ticker, name or CIK), `limit` |
| `edgar_company_profile` | Industry, fiscal year end, exchanges, filer category | `company` |
| `edgar_list_filings` | What did they file, and when? How many? | `company`, `form_types`, `filed_after`, `filed_before`, `include_amendments`, `limit`, `offset`, `response_format` |
| `edgar_get_financial_metric` | What figure did they report for a period? | `company`, `metric`, `fiscal_year`, `fiscal_period` |

Every tool takes a ticker (`AAPL`), a name (`Apple Inc.`) or a CIK (`320193`)
as `company`. All four are marked read-only.

### Design choices

These follow Anthropic's
[Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents);
the evaluation checks that they pay off.

- **Shaped around tasks, not endpoints.** Finding a company's latest 10-K
  through raw endpoints takes a 777 KiB ticker file and a 160 KiB columnar
  JSON document. Here it is one call with `form_types=["10-K"]` and
  `limit=1`.
- **Identifiers people use.** Tickers and names resolve to CIKs inside the
  server. A name that fits several companies is an error that lists them,
  because silently picking the wrong company is the worst failure in
  finance.
- **Small responses.** Filters, paging (`offset`, `next_offset`), a
  `total_matching` count so counting needs no paging, compact JSON, and a
  `concise`/`detailed` switch. The largest designed response in the
  evaluation is 1.2 KiB.
- **Domain rules live in the tool.** In XBRL company-concept data, `fy` is
  the fiscal year of the *filing*, so a fiscal-2025 10-K's 2024 and 2023
  comparatives are also tagged `fy=2025`. Taking the first match returns the
  wrong year. `edgar_get_financial_metric` takes each filing's own period and
  returns its dates, so a figure can be cited.
- **Errors that say what to do next.**
  `No company matches 'Appel'. Did you mean: Apple Inc. (CIK 320193, AAPL)?`
  and `Apple Inc. has no FY value of revenue for fiscal year 1999. Fiscal
  years available: 2009-2026.`
- **A good citizen of EDGAR.** A declared User-Agent is required, as the
  SEC's [fair access policy](https://www.sec.gov/os/accessing-edgar-data)
  asks; requests are rate limited (5 per second by default, against the
  SEC's ceiling of 10), retried with backoff that honours `Retry-After`, and
  cached.

## Evaluation

24 questions about 6 companies (Apple, Microsoft, Walmart, Etsy, Snap-on,
Hormel Foods), four of each kind:

| Kind | Example |
| --- | --- |
| Latest annual report | What is the accession number of the most recent 10-K that Snap-on filed on or before 2026-11-18? |
| Quarterly filing date | On what date did Walmart file its 10-Q for the period ended 2026-07-31? |
| Count | How many 8-Ks did Hormel Foods file from 2025-01-01 through 2025-06-30? |
| Annual figure | What cash and cash equivalents did Microsoft report for fiscal year 2026 in its 10-K? |

The answers change with every filing, and the figures come from reports
filed between December 2025 and November 2026, so they are unlikely to be
answerable from memory. **Ground truth is computed
independently of the server**: it finds a figure by the 10-K's accession
number, where the server groups by fiscal year, and a test checks that both
agree on all 24.

Each sample starts the real server as a subprocess and talks MCP to it over
stdio, so the measurement covers what a client sees: tool list, schemas,
descriptions, responses and errors. Tool output is held to an explicit
budget per call, because Inspect does not truncate MCP results and real
clients do. The default, 100,000 bytes, is roughly the 25,000-token limit
Claude Code applies to tool responses.

### Reference agent (scripted, not a language model)

The scripted agent recognizes the question templates and plays one fixed
strategy per tool surface. On the raw surface it reads JSON the way an ideal
reader would: it aligns columnar arrays by index and applies the correct
fiscal-year rule, from whatever part of a response it was shown. That makes
its baseline accuracy an upper bound. Token counts are character-based
estimates. Generated 2026-11-18 with `inspect-ai` 0.3.289; full tables in
[results/reference/results.md](results/reference/results.md).

| Tool surface | Output budget | Accuracy | Calls / question | Tokens / question | Tool output / question | Calls over budget |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `designed` | 100,000 B | **100%** | 1.00 | **430** | 0.5 KiB | 0% |
| `designed` | 16,384 B | **100%** | 1.00 | 430 | 0.5 KiB | 0% |
| `baseline` | 100,000 B | 100% | 2.00 | 70,927 | 903.9 KiB | 87.5% |
| `baseline` | 16,384 B | **0%** | 1.50 | 8,614 | 843.1 KiB | 100% |

What this shows:

- **Same answers, 165 times the context.** An ideal reader can get every
  answer out of the raw endpoints, at about 71,000 tokens a question
  instead of 430.
- **Raw tools work only when the client's limit is generous.** At 16 KiB,
  Inspect's default for other tools, the part of each response the agent
  sees never contains what it needs, and accuracy falls to zero. The
  designed surface does not notice the difference.
- **What it cannot show** is how well a model reads the descriptions, uses
  the parameters and recovers from the errors. That is what the model run
  measures, and the scripted reader's perfect JSON parsing flatters the
  baseline.

### Models

Not run yet:

```bash
pip install "edgar-mcp[eval] @ git+https://github.com/akarsh323/edgar-mcp"
export ANTHROPIC_API_KEY=...   # or any provider Inspect supports
edgar-mcp-eval run --model anthropic/claude-sonnet-5-5 --epochs 3
edgar-mcp-eval report logs/ --out results/models
```

Runs answer from the bundled snapshot, so they make no requests to the SEC
and are reproducible.

## Quick start

```bash
pip install git+https://github.com/akarsh323/edgar-mcp
export EDGAR_MCP_USER_AGENT="Your Name you@example.org"   # the SEC requires it
edgar-mcp                                                  # MCP over stdio
```

**Claude Code**

```bash
claude mcp add --transport stdio edgar \
  --env EDGAR_MCP_USER_AGENT="Your Name you@example.org" -- edgar-mcp
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "edgar": {
      "command": "edgar-mcp",
      "env": { "EDGAR_MCP_USER_AGENT": "Your Name you@example.org" }
    }
  }
}
```

**Offline**, from the bundled snapshot, with no requests to the SEC:
`edgar-mcp --snapshot bundled`. **Over HTTP**:
`edgar-mcp --transport streamable-http --port 8000`. **To explore the
tools** in a browser:
`npx @modelcontextprotocol/inspector edgar-mcp --snapshot bundled`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `EDGAR_MCP_USER_AGENT` | none | Your name and contact address. Required for live requests. |
| `EDGAR_MCP_SNAPSHOT` | live | A snapshot directory, or `bundled`. |
| `EDGAR_MCP_TOOLSET` | `designed` | `baseline` exposes the raw wrappers, for comparison only. |
| `EDGAR_MCP_MAX_REQUESTS_PER_SECOND` | `5` | Live request rate, at most 10. |

## Data

The bundled snapshot holds 73 responses from the SEC's
[public APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces),
recorded on 2026-11-18 and stored byte for byte, gzip compressed (481 KiB):
the full ticker file, the six companies' filing indexes, and their XBRL
concepts for the eight supported figures.
[manifest.json](src/edgar_mcp/data/snapshot/manifest.json) lists what is in
it. `make snapshot` re-records it.

## Limitations

- **Recent filings only.** The SEC's recent-filings index holds at least the
  last year or the last 1,000 filings, whichever is more. Older pages are
  not read.
- **Eight US-GAAP figures.** Companies filing under IFRS (20-F, 40-F) and
  figures outside the eight are not covered.
- **Fiscal years are the companies' own labels.** Walmart's fiscal 2026
  ended on 2026-01-31.
- **24 questions** is a probe, not a benchmark. Intervals resample questions.
- **The scripted agent is an idealized reader,** and its token counts are
  estimates. The model run is the real test of the descriptions and errors.

## Development

```bash
git clone https://github.com/akarsh323/edgar-mcp && cd edgar-mcp
python -m venv .venv && source .venv/bin/activate
make install
make check        # ruff, mypy --strict, pytest (includes MCP stdio runs)
make reference    # regenerate results/reference
```

```
src/edgar_mcp/
  config.py        settings from the environment
  client.py        HTTP: rate limit, retries, cache, snapshot transport
  edgar.py         company resolution, filings, the fiscal-year rule
  server.py        the MCP server: designed and baseline tool surfaces
  __main__.py      edgar-mcp command
  evals/
    questions.py   questions and independently computed answers
    task.py        Inspect task: agent loop over MCP stdio, budget, scorer
    reference.py   scripted agent as an Inspect model provider
    cli.py         edgar-mcp-eval run | report
scripts/record_snapshot.py
```

[docs/design.md](docs/design.md) has the reasoning behind each choice and the
alternatives that were rejected.

## License

Apache 2.0. EDGAR data is public information published by the U.S.
Securities and Exchange Commission. This project is not affiliated with the
SEC.
