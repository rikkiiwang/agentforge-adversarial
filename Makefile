# AgentForge Adversarial — Makefile
#
# All commands pin the venv interpreter so reviewers don't need to remember
# to `source .venv/bin/activate`.

PY     := .venv/bin/python
PIP    := .venv/bin/pip
PYTEST := .venv/bin/pytest
ST     := .venv/bin/streamlit

PYTHON311 := /opt/homebrew/bin/python3.11

.PHONY: help venv install init-db test run run-mutate run-live dashboard clean-db logs ps health

help:
	@echo "AgentForge Adversarial — make targets"
	@echo ""
	@echo "  make venv           Create .venv with Python 3.11"
	@echo "  make install        pip install -e \".[dev]\" into the venv"
	@echo "  make up             docker compose up -d (Postgres on :5433)"
	@echo "  make init-db        Apply migrations/001_initial.sql"
	@echo ""
	@echo "  make test           Run the full test suite"
	@echo "  make run            Run a campaign against MockCopilotClient"
	@echo "  make run-mutate     Run + generate 3 LLM mutations per seed"
	@echo "  make run-live       Run against the deployed Co-Pilot (needs COPILOT_SESSION_ID)"
	@echo "  make dashboard      Launch Streamlit on http://localhost:8501"
	@echo ""
	@echo "  make health         Probe live target health"
	@echo "  make clean-db       TRUNCATE all tables (fresh demo state)"
	@echo "  make ps             docker compose ps"
	@echo "  make logs           Tail Postgres logs"

venv:
	$(PYTHON311) -m venv .venv
	$(PIP) install --upgrade pip

install:
	$(PIP) install -e ".[dev]"

up:
	docker compose up -d

init-db:
	$(PY) -m agentforge_adversarial init-db

test:
	$(PYTEST) -v

run:
	$(PY) -m agentforge_adversarial run --cases evals/cases

run-mutate:
	$(PY) -m agentforge_adversarial run --cases evals/cases --mutate

run-live:
	@if [ -z "$$COPILOT_SESSION_ID" ]; then \
	  echo "ERROR: COPILOT_SESSION_ID is not set."; \
	  echo "Obtain a session_id by opening the Co-Pilot iframe in OpenEMR and"; \
	  echo "copying the value from the POST /v1/sessions response in devtools."; \
	  echo "Then: export COPILOT_SESSION_ID=<uuid>"; \
	  exit 1; \
	fi
	$(PY) -m agentforge_adversarial run --cases evals/cases --mutate --live

dashboard:
	$(ST) run dashboard/app.py

health:
	@echo "Live target health:"
	@curl -sS https://copilot-production-b532.up.railway.app/healthz
	@echo ""

clean-db:
	docker compose exec -T postgres psql -U agentforge -d agentforge \
	  -c "TRUNCATE attack_runs, attack_queue, campaigns CASCADE;"

ps:
	docker compose ps

logs:
	docker compose logs -f postgres
