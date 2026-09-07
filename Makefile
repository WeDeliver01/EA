.PHONY: up down logs migrate check lint typecheck imports purity test test-unit test-integration test-golden coverage fmt

COMPOSE = docker compose -f infra/docker-compose.yml --env-file .env

up:
	$(COMPOSE) up -d --build
	@echo "Waiting for API..."
	@for i in $$(seq 1 30); do \
		curl -sf http://127.0.0.1:8000/api/v1/system/ready > /dev/null && echo "ready" && exit 0; \
		sleep 1; \
	done; \
	echo "API did not become ready in time" && exit 1

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

migrate:
	cd backend && alembic upgrade head

lint:
	cd backend && ruff check . && ruff format --check .

fmt:
	cd backend && ruff check --fix . && ruff format .

typecheck:
	cd backend && mypy --strict app/domain app/engines
	cd backend && mypy app

imports:
	cd backend && lint-imports

purity:
	python3 scripts/check_purity.py

test-unit:
	cd backend && pytest tests/unit -m unit -q

test-integration:
	cd backend && pytest tests/integration -m integration -q

test-golden:
	cd backend && pytest tests/golden -m golden -q

test:
	cd backend && pytest -q

coverage:
	cd backend && pytest --cov=app --cov-branch --cov-report=term-missing

check: lint typecheck imports purity test
	@echo "All checks passed."
