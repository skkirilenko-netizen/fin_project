.PHONY: db-schema probes db-reset test lint fmt check check-conclusion llm-stats report analyze ingest pdf-check regression regression-full ifrs-set ifrs-regression

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

pdf-check:  ## Проверить PDF до выгрузки в проект: make pdf-check FILE="путь/к/отчётности.pdf"
	uv run fin-analysis pdf-check $(FILE) $(ARGS)

ingest:  ## Загрузить поданные вручную файлы из data/inbox: make ingest ARGS="--inn 7736050003"
	uv run fin-analysis ingest $(ARGS)

regression:  ## Регрессионный прогон набора без модели: make regression ARGS="--fetch"
	uv run python eval/regression_run.py --contour fast $(ARGS)

regression-full:  ## Тот же набор с генерацией текста: минуты на организацию
	uv run python eval/regression_run.py --contour full $(ARGS)

ifrs-set:  ## Состав набора МСФО: сколько документов выгружать и что они покрывают
	uv run python eval/ifrs_set.py

ifrs-regression:  ## Прогон набора МСФО без модели: make ifrs-regression ARGS=--write
	uv run python eval/ifrs_regression_run.py $(ARGS)

report:  ## Сформировать заключение в docx: make report INN=7736050003
	uv run python -c "from finlib.report.document import build_report; \
	print(build_report('$(INN)').path)"

analyze:  ## Полный цикл по ИНН: make analyze INN=7736050003 ARGS=--llm
	uv run fin-analysis analyze --inn $(INN) $(ARGS)
