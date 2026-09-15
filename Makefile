.PHONY: db-schema probes db-reset test lint fmt check check-conclusion llm-stats report

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

check-conclusion:  ## Прогнать модель на ИНН и разобрать постпроверку: make check-conclusion INN=7736050003
	uv run python eval/conclusion_check.py $(INN) $(ARGS)

llm-stats:  ## Статистика обращений к модели по журналу llm_log
	uv run python eval/llm_stats.py

report:  ## Сформировать заключение в docx: make report INN=7736050003
	uv run python -c "from finlib.report.document import build_report; \
	print(build_report('$(INN)').path)"
