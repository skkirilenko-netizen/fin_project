.PHONY: db-schema test lint fmt check

db-schema:  ## Применить схему БД
	psql findb -f sql/001_schema.sql

test:  ## Прогнать тесты
	uv run pytest

lint:  ## Проверить стиль
	uv run ruff check .

fmt:  ## Отформатировать код
	uv run ruff format .

check: lint test  ## Линтер и тесты
