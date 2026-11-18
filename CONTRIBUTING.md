# Contributing

Issues and pull requests are welcome.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
make install
make check
```

`make check` is what CI runs: `ruff`, `mypy --strict`, and `pytest` with a
90% coverage floor. Tests need no API key and make no requests to the SEC;
they use the bundled snapshot.

## Changing a tool

Tool names, descriptions, parameters and response shapes are what the
evaluation measures, so:

1. Change the tool in `server.py`.
2. Run `make reference` and compare `results/reference` before and after.
3. If you have API access, run `edgar-mcp-eval run --model <model>` on both
   versions and include the before and after tables in the pull request.
4. Bump `TASK_VERSION` in `evals/task.py` if the questions, scoring or the
   output budget change.

## Changing the data

Do not edit files under `src/edgar_mcp/data/snapshot/` by hand. Re-record
with `make snapshot`, regenerate the questions with
`python -c "from edgar_mcp.evals.questions import *; dump_questions(generate_questions(), ...)"`,
and say so in the pull request: answers change with every new filing.

Set `EDGAR_MCP_USER_AGENT` to your own name and contact address when
recording. The SEC requires it; the recorder does not store it.

## Style

Google Python style: docstrings with `Args`/`Returns`/`Raises`, type
annotations everywhere, 80 columns. `make format` applies it.
