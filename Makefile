# AgentForge Adversarial — Makefile
#
# All commands pin the venv interpreter so reviewers don't need to remember
# to `source .venv/bin/activate`.

PY     := .venv/bin/python
PIP    := .venv/bin/pip
PYTEST := .venv/bin/pytest
ST     := .venv/bin/streamlit

PYTHON311 := /opt/homebrew/bin/python3.11

.PHONY: help venv install init-db test run run-no-mutate run-live dashboard clean-db logs ps health

help:
	@echo "AgentForge Adversarial — make targets"
	@echo ""
	@echo "  make venv           Create .venv with Python 3.11"
	@echo "  make install        pip install -e \".[dev]\" into the venv"
	@echo "  make up             docker compose up -d (Postgres on :5433)"
	@echo "  make init-db        Apply migrations/001_initial.sql"
	@echo ""
	@echo "  make test           Run the full test suite"
	@echo "  make run            Run a campaign against the mock target (no env vars needed)"
	@echo "  make run-no-mutate  Run seeds only (mutator off; mutator is on by default)"
	@echo "  make run-live       Run against the deployed Co-Pilot (needs COPILOT_PATIENT_ID)"
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

# Default: in-process MockCopilotClient via the seeded mock target row
# (`migrations/006_mock_target.sql`). No env vars, no network. Use this
# for first-run / offline demos.
run:
	$(PY) -m agentforge_adversarial run --cases evals/cases \
	  --target "Mock Co-Pilot (local stub)"

# Seeds-only (mutator off). Mutator is on by default — use --no-mutate
# to disable it. Live vs mock is chosen by --target.
run-no-mutate:
	$(PY) -m agentforge_adversarial run --cases evals/cases --no-mutate \
	  --target "Mock Co-Pilot (local stub)"

# Live = the seeded deployed Co-Pilot target. Requires COPILOT_PATIENT_ID
# (a Synthea UUID) — make_client() reads it as a fallback when the target
# row's config_json.patient_id is empty.
run-live:
	@if [ -z "$$COPILOT_PATIENT_ID" ]; then \
	  echo "ERROR: COPILOT_PATIENT_ID is not set."; \
	  echo "The harness auto-creates sessions; it just needs to know which"; \
	  echo "Synthea patient to anchor the session to. Grab a patient UUID"; \
	  echo "from the OpenEMR patient list (the 'pid' query param), then:"; \
	  echo "  export COPILOT_PATIENT_ID=<uuid>"; \
	  exit 1; \
	fi
	$(PY) -m agentforge_adversarial run --cases evals/cases \
	  --target "OpenEMR Clinical Co-Pilot (deployed)"

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
