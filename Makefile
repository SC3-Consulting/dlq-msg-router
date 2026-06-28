VENV ?= .venv
PYTHON ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,$(shell command -v python3 || command -v python))
COVERAGE_FAIL_UNDER ?= 85
SKIP_LLM_CHECK ?= 0
PYLINT_ARGS ?= --errors-only
MYPY_ARGS ?= --ignore-missing-imports --explicit-package-bases --disable-error-code import-untyped --disable-error-code assignment --disable-error-code arg-type --disable-error-code index
HOTSPOTS_FILE ?= tests/test_dlq_pipeline.py
HOTSPOTS_DURATIONS ?= 20
HOTSPOTS_K ?=
HOTSPOTS_TRACE ?= 0
PERF_SIZES ?= 100,500,1000
PERF_AI_RATIO ?= 0.20
PERF_OUTPUT ?= reports/performance_baseline.json
OPS_RESOURCE_GROUP ?= rg-dlq-msg-router-dev
OPS_CONTAINER_APP ?= ca-dlq-msg-router-dev
OPS_LOOKBACK_MINUTES ?= 60
OPS_TARGET_REVISION ?=

.PHONY: help install-dev test test-cov test-hotspots perf-baseline ops-triage-snapshot ops-rollback format format-check lint type-check quality local-llm-check local-up local-up-emulator local-smoke-emulator local-down

help:
	@echo "Available targets:"
	@echo "  install-dev   Install runtime and development dependencies"
	@echo "  test          Run test suite without coverage gate"
	@echo "  test-cov      Run test suite with coverage and fail-under gate"
	@echo "  test-hotspots Profile slow tests (supports HOTSPOTS_FILE, HOTSPOTS_DURATIONS, HOTSPOTS_K, HOTSPOTS_TRACE=1)"
	@echo "  perf-baseline Run local performance baseline benchmark (PERF_SIZES, PERF_AI_RATIO, PERF_OUTPUT)"
	@echo "  ops-triage-snapshot Capture app state, active revisions, and recent logs (OPS_RESOURCE_GROUP, OPS_CONTAINER_APP, OPS_LOOKBACK_MINUTES)"
	@echo "  ops-rollback  Roll back container app to a target (or latest inactive) revision (OPS_RESOURCE_GROUP, OPS_CONTAINER_APP, OPS_TARGET_REVISION)"
	@echo "  format        Auto-format code with isort and black"
	@echo "  format-check  Validate formatting with isort and black"
	@echo "  lint          Run pylint"
	@echo "  type-check    Run mypy"
	@echo "  quality       Run format-check, lint, and type-check"
	@echo "  local-llm-check Recommend OLLAMA_MODEL from detected GPU/VRAM (best-effort)"
	@echo "  (set SKIP_LLM_CHECK=1 to bypass startup recommendation checks)"
	@echo "  local-up      Start local agent stack (no emulator profile)"
	@echo "  local-up-emulator Start local stack with ASB emulator profile (requires ACCEPT_EULA=Y)"
	@echo "  local-smoke-emulator Run local emulator smoke test (inject DLQ message and verify processing)"
	@echo "  local-down    Stop local compose stack"

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest tests

test-cov:
	$(PYTHON) -m pytest tests --cov=src --cov-report=term-missing --cov-fail-under=$(COVERAGE_FAIL_UNDER)

test-hotspots:
	@if [ "$(HOTSPOTS_TRACE)" = "1" ]; then \
		echo "Running hotspot profile with tracemalloc (25 frames)..."; \
		PYTHONTRACEMALLOC=25 $(PYTHON) -m pytest $(HOTSPOTS_FILE) -q --durations=$(HOTSPOTS_DURATIONS) $(if $(HOTSPOTS_K),-k "$(HOTSPOTS_K)",); \
	else \
		echo "Running hotspot profile (durations only)..."; \
		$(PYTHON) -m pytest $(HOTSPOTS_FILE) -q --durations=$(HOTSPOTS_DURATIONS) $(if $(HOTSPOTS_K),-k "$(HOTSPOTS_K)",); \
	fi

perf-baseline:
	PYTHONPATH=. $(PYTHON) scripts/performance_baseline.py --sizes "$(PERF_SIZES)" --ai-ratio "$(PERF_AI_RATIO)" --output "$(PERF_OUTPUT)"

ops-triage-snapshot:
	bash ./scripts/runbook_triage_snapshot.bash "$(OPS_RESOURCE_GROUP)" "$(OPS_CONTAINER_APP)" "$(OPS_LOOKBACK_MINUTES)"

ops-rollback:
	bash ./scripts/runbook_rollback.bash "$(OPS_RESOURCE_GROUP)" "$(OPS_CONTAINER_APP)" "$(OPS_TARGET_REVISION)"

format:
	$(PYTHON) -m isort --profile black src tests
	$(PYTHON) -m black src tests

format-check:
	$(PYTHON) -m isort --profile black --check-only src tests
	$(PYTHON) -m black --check src tests

lint:
	$(PYTHON) -m pylint src $(PYLINT_ARGS)

type-check:
	$(PYTHON) -m mypy src $(MYPY_ARGS)

quality: format-check lint type-check

local-llm-check:
	@if [ "$(SKIP_LLM_CHECK)" = "1" ]; then \
		echo "Skipping LLM startup check (SKIP_LLM_CHECK=1)"; \
		exit 0; \
	fi
	./scripts/ollama_model_startup_check.sh

local-up:
	@if [ "$(SKIP_LLM_CHECK)" != "1" ]; then ./scripts/ollama_model_startup_check.sh || true; else echo "Skipping LLM startup check (SKIP_LLM_CHECK=1)"; fi
	docker compose up -d --build

local-up-emulator:
	@if [ "$(SKIP_LLM_CHECK)" != "1" ]; then ./scripts/ollama_model_startup_check.sh || true; else echo "Skipping LLM startup check (SKIP_LLM_CHECK=1)"; fi
	docker compose --profile asb-emulator up -d --build

local-smoke-emulator:
	docker compose exec -T dlq-agent python -m src.local_smoke_test

local-down:
	docker compose down
