# Developer entry points. Everything runs inside the project venv.
PY ?= .venv/bin/python
STRICT := tlgr/models tlgr/ops tlgr/registry.py tlgr/schema.py tlgr/version.py \
          tlgr/parity.py \
          tlgr/core/errors.py tlgr/core/timefmt.py tlgr/core/pagination.py \
          tlgr/core/config.py tlgr/core/paths.py tlgr/core/peers.py \
          tlgr/core/logging.py tlgr/core/identity.py tlgr/core/media.py \
          tlgr/transport \
          tlgr/daemon/session.py tlgr/daemon/sessions.py tlgr/daemon/events.py \
          tlgr/daemon/preauth.py tlgr/daemon/dispatch.py \
          tlgr/daemon/ratelimit.py tlgr/daemon/policy.py tlgr/daemon/idle.py \
          tlgr/daemon/singleton.py tlgr/daemon/peercred.py \
          tlgr/daemon/files.py tlgr/daemon/transfers.py

.PHONY: lint format typecheck test test-fast check docs parity acceptance

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

# The subset that proves ARCHITECTURE §12.3. `docs/design/FOUNDATION_ACCEPTANCE.md`
# maps each of the 20 criteria to a name in here; run it when you want the
# answer to "is the foundation done" without waiting for the whole suite.
ACCEPTANCE := tests/test_agentmd_compat.py tests/test_registry_contract.py \
              tests/test_ops_message.py tests/test_ops_draft.py \
              tests/test_ops_auth.py tests/test_account_alias_resolution.py \
              tests/test_ops_media.py tests/test_ops_sticker.py \
              tests/test_cli_mapping.py tests/test_security.py \
              tests/test_daemon_lifecycle.py tests/test_account_session.py \
              tests/test_daemon_connection_health.py tests/test_stream.py \
              tests/test_events.py tests/test_webhook.py \
              tests/test_errors_map.py tests/test_dispatch.py \
              tests/test_sandbox.py tests/test_schema.py \
              tests/test_docs_fresh.py tests/test_parity.py \
              tests/test_transport.py

acceptance:
	$(PY) -m pytest -q $(ACCEPTANCE)
