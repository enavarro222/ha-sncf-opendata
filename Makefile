.PHONY: venv lint test check

# Heavy jobs are run through musette-limited when it exists (resource limits).
LIMIT := $(shell command -v musette-limited 2>/dev/null)
PY := .venv/bin/python

venv:
	uv venv --python 3.14 .venv
	uv pip install --python $(PY) -r requirements-dev.txt

lint:
	$(PY) -m ruff format --check custom_components tests
	$(PY) -m ruff check custom_components tests
	$(LIMIT) $(PY) -m mypy

test:
	$(LIMIT) $(PY) -m pytest -q --cov=custom_components/sncf_opendata --cov-report=term-missing

check: lint test
