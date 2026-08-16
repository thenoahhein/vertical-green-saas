up:
	docker compose -f infra/compose.yml up -d --build
down:
	docker compose -f infra/compose.yml down
migrate:
	docker compose -f infra/compose.yml run --rm api uv run alembic -c apps/api/alembic.ini upgrade head
seed:
	docker compose -f infra/compose.yml run --rm api python seed.py
test:
	uv run pytest apps/api/tests
lint:
	uv run ruff check apps/api apps/worker seed.py
typecheck:
	uv run mypy apps/api/src apps/worker/src
