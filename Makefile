CASES ?= eval/cases/public

-include .env
export

.PHONY: setup seed truth api up down test lint validate-cases check-spec eval

setup: .env
	uv sync

.env:
	cp .env.example .env

seed: .env
	uv run python mock-bank-api/seed/generate.py

data/bank.db:
	$(MAKE) seed

truth: data/bank.db
	uv run python scripts/compute_truth.py

api: .env data/bank.db
	uv run uvicorn --app-dir mock-bank-api app.main:app --host 127.0.0.1 --port 8001

# Extend docker-compose.yml so that this also starts your services.
up: .env
	docker compose up -d --build

down:
	docker compose down

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

validate-cases: truth
	uv run python scripts/validate_cases.py eval/cases

check-spec:
	bash scripts/check_spec_first.sh

# Replace this recipe with your harness. It must run any folder of cases (EV-1).
eval:
	@echo "make eval: the evaluation harness is not implemented yet."
	@echo "You must implement it in eval/harness/ (see EV-1 to EV-9 in README.md)."
	@echo "It must run: make eval CASES=$(CASES)"
	@exit 1
