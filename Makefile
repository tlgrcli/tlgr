# Developer entry points. Everything runs inside the project venv.
PY ?= .venv/bin/python
STRICT := tlgr/models tlgr/ops tlgr/registry.py tlgr/schema.py \
          tlgr/core/errors.py tlgr/core/timefmt.py tlgr/core/pagination.py

.PHONY: lint format typecheck test test-fast check

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy $(STRICT)

test:
	$(PY) -m pytest -q --cov=tlgr --cov-report=term-missing

# No coverage instrumentation: the inner-loop run.
test-fast:
	$(PY) -m pytest -q -x

check: lint typecheck test
