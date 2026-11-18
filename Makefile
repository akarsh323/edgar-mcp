.PHONY: help install lint format types test check reference snapshot serve clean

help:  ## Show this help.
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install:  ## Install the package with eval and dev tools.
	pip install -e . -r requirements-dev.txt

lint:  ## Lint and check formatting.
	ruff check .
	ruff format --check .

format:  ## Apply formatting and safe lint fixes.
	ruff format .
	ruff check --fix .

types:  ## Type-check with mypy --strict.
	mypy

test:  ## Run the tests, including end-to-end runs over MCP stdio.
	pytest --cov=edgar_mcp --cov-report=term-missing

check: lint types test  ## Everything CI runs.

reference:  ## Regenerate results/reference with the scripted agent.
	rm -rf logs/reference
	edgar-mcp-eval run --model edgar_mcp/scripted --toolset both \
		--max-tool-output 100000 --max-tool-output 16384 \
		--log-dir logs/reference --display none
	edgar-mcp-eval report logs/reference --out results/reference

snapshot:  ## Re-record the bundled snapshot (needs EDGAR_MCP_USER_AGENT).
	python scripts/record_snapshot.py

serve:  ## Run the server offline over stdio, from the bundled snapshot.
	edgar-mcp --snapshot bundled

clean:  ## Remove caches and build output.
	rm -rf build dist .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
