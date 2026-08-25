.PHONY: help install test lint fmt server agent evals mock-api inspect clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## install the package with dev extras
	python -m pip install -e ".[dev]"

test:  ## run the test suite (no API key required)
	pytest -q

lint:  ## check formatting and imports
	ruff check .

fmt:  ## auto-fix lint issues
	ruff check --fix .

server:  ## run the MCP server on stdio
	python -m snow_mcp.server

mock-api:  ## run the ServiceNow-compatible mock Table API
	snow-mock-api --port 8080

agent:  ## one-shot agent run: make agent TASK="..."
	snow-agent -v "$(TASK)"

evals:  ## run the full eval suite
	snow-evals --concurrency 4

inspect:  ## open the MCP inspector against the server
	npx @modelcontextprotocol/inspector python -m snow_mcp.server

clean:
	rm -rf .pytest_cache .ruff_cache runs build dist *.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} +
