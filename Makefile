.PHONY: db-schema probes db-reset test lint fmt check check-conclusion llm-stats report analyze ingest pdf-check regression regression-full ifrs-set ifrs-regression ifrs-reference ifrs-scale ifrs-synonyms

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

ifrs-reference:  ## Эталонные величины по настоящим документам: расхождение — остановка
	uv run python eval/ifrs_reference_run.py $(ARGS)

events:  ## Событийный слой: дефолты, рейтинги, рефинансирование (только чтение)
	uv run python eval/events_run.py $(ARGS)

emissions-fetch:  ## Забрать выпуски по всем эмитентам справочника (977 запросов)
	uv run python scripts/emissions_fetch.py $(ARGS)

okved-fetch:  ## Основной вид деятельности эмитентов из ГИР БО: правило холдинга
	uv run python scripts/okved_fetch.py $(ARGS)

routing-reference:  ## Эталон списка наблюдения, уровень проекта: расхождение — остановка
	uv run python eval/routing_reference_run.py $(ARGS)

watchlist:  ## Собрать список наблюдения в data/output/watchlist_<дата>.html
	uv run python eval/watchlist_run.py $(ARGS)

daily:  ## Ежедневный прогон целиком: доставка, маршрут, список, отчёт изменений
	uv run python scripts/daily_run.py $(ARGS)

backfill:  ## История корзин: пересчёт маршрута назад за год (--write — с записью)
	uv run python eval/routing_backfill_run.py $(ARGS)

changes:  ## Отчёт «что изменилось»: последняя пара точек истории
	uv run python eval/change_report_run.py $(ARGS)

history-measure:  ## Движение корзин по пересчитанной истории и его дребезг
	uv run python eval/history_measure_run.py $(ARGS)

card:  ## Карточка эмитента: make card ARGS="9703024202" либо ARGS=--all
	uv run python eval/issuer_card_run.py $(ARGS)

refinancing-gap:  ## Зазор у границы: цена двух способов унять дребезг (только замер)
	uv run python eval/refinancing_gap_run.py $(ARGS)

iss-history:  ## Доставка дневных срезов ISS: make iss-history ARGS="--depth-days 730 --step 1"
	uv run python scripts/moex_market_fetch.py $(ARGS)

iss-summary:  ## Что доставлено с ISS: глубина, поля, место на диске
	uv run python eval/iss_history_run.py $(ARGS)

market-lead:  ## Упреждение слоёв: рынок против отчётности и рейтингов (замер)
	uv run python eval/market_lead_run.py > data/output/market_lead.md
	@echo "data/output/market_lead.md"

ratings-history:  ## Календарь рейтинговых действий: упреждение, тревоги, понижения
	uv run python eval/ratings_history_run.py $(ARGS)

lsr-debt:  ## Долговая нагрузка ЛСР: документ против агрегатора, построчно
	uv run python eval/lsr_debt_run.py $(ARGS)

watchlist-coverage:  ## Охват списка: кого он видит и кого не видит по построению
	uv run python eval/watchlist_coverage_run.py $(ARGS)

event-measure:  ## Календарь событий и замер маршрута без событийного правила
	uv run python eval/event_measure_run.py $(ARGS)

ratings-snapshot:  ## Снимок рейтингов на сегодня (ежедневно; один запрос на эмитента)
	uv run python scripts/ratings_snapshot.py $(ARGS)

ifrs-scale:  ## Масштаб разметки: сколько работы одного эмитента достаётся другим
	uv run python eval/ifrs_markup_scale.py

ifrs-synonyms:  ## Наименования на подъём в справочник: присвоено человеком, разбором не опознано
	uv run python eval/ifrs_synonym_candidates.py $(ARGS)

report:  ## Сформировать заключение в docx: make report INN=7736050003
	uv run python -c "from finlib.report.document import build_report; \
	print(build_report('$(INN)').path)"

analyze:  ## Полный цикл по ИНН: make analyze INN=7736050003 ARGS=--llm
	uv run fin-analysis analyze --inn $(INN) $(ARGS)
