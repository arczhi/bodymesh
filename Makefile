.PHONY: help install test lint brief daily clean

help:
	@grep -E '^[a-z-]+:.*?##' Makefile | sed 's/:.*##/\t/'

install:  ## create .venv and install in editable mode
	uv venv .venv --python 3.12
	. .venv/bin/activate && uv pip install -e ".[dev]"

test:  ## run the test suite
	. .venv/bin/activate && python -m pytest -q

brief:  ## render yesterday's brief
	. .venv/bin/activate && phy daily

daily:  ## the 09:00 job
	. .venv/bin/activate && phy daily --json

clean:  ## remove caches
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
