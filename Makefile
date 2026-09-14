.PHONY: db-schema probes db-reset test lint fmt check

db-schema:  ## Применить схему БД
	psql findb -f sql/001_schema.sql

probes:  ## Перезалить сохранённые пробы ГИР БО (сеть не используется)
	uv run python scripts/load_probes.py

db-reset: db-schema probes  ## Применить схему и восстановить рабочий набор данных

test:  ## Прогнать тесты
	uv run pytest

lint:  ## Проверить стиль
	uv run ruff check .

fmt:  ## Отформатировать код
	uv run ruff format .

check: lint test  ## Линтер и тесты
