# Developer entry points. Everything runs inside the project venv.
PY ?= .venv/bin/python
STRICT := tlgr/models tlgr/ops tlgr/registry.py tlgr/schema.py tlgr/version.py \
          tlgr/parity.py \
          tlgr/core/errors.py tlgr/core/timefmt.py tlgr/core/pagination.py \
          tlgr/core/config.py tlgr/core/paths.py tlgr/core/peers.py \
          tlgr/core/logging.py tlgr/core/identity.py tlgr/core/media.py \
          tlgr/transport \
          tlgr/daemon/session.py tlgr/daemon/sessions.py tlgr/daemon/events.py \
          tlgr/daemon/ratelimit.py tlgr/daemon/policy.py tlgr/daemon/idle.py \
          tlgr/daemon/singleton.py tlgr/daemon/peercred.py

.PHONY: lint format typecheck test test-fast check docs parity

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

# The parity index and the reference docs are generated; CI runs the --check
# form so a stale artefact fails the build instead of drifting quietly.
parity:
	$(PY) tools/prune_catalog.py
	$(PY) tools/gen_docs.py --parity

docs:
	$(PY) tools/gen_docs.py

check: lint typecheck test docs parity
