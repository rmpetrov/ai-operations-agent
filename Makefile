.PHONY: db-up db-down migrate seed run test lint

db-up:
	docker compose up -d db

db-down:
	docker compose down

migrate:
	alembic upgrade head

seed:
	python -m ai_operations_agent.seed

run:
	uvicorn ai_operations_agent.main:app --reload

test:
	pytest -q

lint:
	ruff check .
