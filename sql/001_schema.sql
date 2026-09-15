-- Схема базы findb. Применение: psql findb -f sql/001_schema.sql
-- Файл идемпотентен: повторное выполнение не меняет состояние.
-- Денежные величины — только numeric. NULL означает «не раскрыто», не ноль.

BEGIN;

-- Справочник организаций -----------------------------------------------------

CREATE TABLE IF NOT EXISTS organization (
    inn         text PRIMARY KEY CHECK (inn ~ '^[0-9]{10}$' OR inn ~ '^[0-9]{12}$'),
    girbo_id    bigint,
    name        text,
    short_name  text,
    ogrn        text,
    okpo        text,
    okved       text,
    region      text,
    meta        jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE organization IS 'Реквизиты организации, полученные вместе с отчётностью';
COMMENT ON COLUMN organization.meta IS 'Прочие реквизиты источника в исходном виде';

-- Источники отчётности -------------------------------------------------------

CREATE TABLE IF NOT EXISTS src_file (
    id                bigserial PRIMARY KEY,
    inn               text NOT NULL REFERENCES organization (inn) ON DELETE CASCADE,
    report_year       integer NOT NULL,
    source            text NOT NULL CHECK (source IN ('gir_bo', 'file')),
    standard          text NOT NULL DEFAULT 'rsbu' CHECK (standard IN ('rsbu', 'ifrs')),
    reporting_type    text NOT NULL DEFAULT 'full'
                      CHECK (reporting_type IN ('full', 'simplified')),
    source_url        text,
    raw_path          text,
    checksum          text,
    form_codes        text[],
    knd               text,
    girbo_bfo_id      bigint,
    correction_version integer NOT NULL DEFAULT 0,
    is_actual         boolean NOT NULL DEFAULT true,
    unit_code         text,
    unit_multiplier   numeric,
    unit_source       text NOT NULL DEFAULT 'unknown'
                      CHECK (unit_source IN ('form_standard', 'explicit', 'unknown')),
    status            text NOT NULL DEFAULT 'loaded'
                      CHECK (status IN ('loaded', 'processed', 'quarantine')),
    quarantine_reason text,
    meta              jsonb,
    loaded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT src_file_uniq UNIQUE (inn, standard, report_year, source, correction_version)
);

CREATE INDEX IF NOT EXISTS src_file_checksum_idx ON src_file (checksum);
CREATE INDEX IF NOT EXISTS src_file_status_idx ON src_file (status);

COMMENT ON TABLE src_file IS 'Загруженная отчётность как единица обработки';
COMMENT ON COLUMN src_file.checksum IS 'sha256 сырого ответа источника';
COMMENT ON COLUMN src_file.unit_code IS 'Единица измерения (ОКЕИ: 384 — тыс. руб., 385 — млн руб.)';
COMMENT ON COLUMN src_file.unit_source IS
    'form_standard — единица определена формой отчётности (перечень форм и код ОКЕИ '
    'в methodology/lines.yaml, блок units); explicit — единица указана источником; '
    'unknown — не определена, комплект уходит в карантин контролем unit_not_determined. '
    'Значения assumed нет: принятая по умолчанию единица — дефект данных, ошибка '
    'в тысячу раз не ловится ни одним контролем';
COMMENT ON COLUMN src_file.standard IS
    'Стандарт отчётности: rsbu — РСБУ отдельного юридического лица, ifrs — консолидированная '
    'по МСФО. Входит в ключ уникальности: за один год организация может раскрыть и то, и другое. '
    'Ветка ifrs пока не реализована, значение заведено, чтобы потом не мигрировать данные';
COMMENT ON COLUMN src_file.knd IS 'Код налогового документа: 0710099 — полная отчётность, 0710096 — упрощённая';
COMMENT ON COLUMN src_file.correction_version IS
    'Номер корректировки отчётности. Входит в ключ уникальности: организация может сдать '
    'несколько версий за один год, и они сохраняются обе';
COMMENT ON COLUMN src_file.is_actual IS
    'Является ли эта корректировка актуальной по данным источника; в расчёт идёт только актуальная';
COMMENT ON COLUMN src_file.unit_multiplier IS 'Коэффициент приведения значений источника к тысячам рублей';
COMMENT ON COLUMN src_file.status IS 'quarantine — данные не прошли контроли качества и в расчёт не идут';
COMMENT ON COLUMN src_file.reporting_type IS
    'Набор строк отчётности: full — полные формы, simplified — упрощённые (приложение 5 к приказу 66н). '
    'Свойство сданного комплекта, а не организации: право на упрощённую отчётность может быть утрачено. '
    'По нему выбирается набор контролей качества и строк справочника';

-- Факты отчётности -----------------------------------------------------------

-- Приоритет источника значения: отчётный период комплекта старше сравнительных.
-- Один и тот же период приходит и как current комплекта 2023 года, и как
-- previous комплекта 2024-го; значения могут расходиться из-за переклассификации.
-- Без приоритета побеждал бы тот, кто загрузился последним.
CREATE OR REPLACE FUNCTION period_rank(role text) RETURNS smallint
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT (CASE role
        WHEN 'current' THEN 0
        WHEN 'previous' THEN 1
        WHEN 'before_previous' THEN 2
    END)::smallint
$$;

COMMENT ON FUNCTION period_rank(text) IS
    'Приоритет периода: 0 — отчётный, 1 — предыдущий, 2 — позапрошлый. Меньше значит важнее';

CREATE TABLE IF NOT EXISTS fact_report (
    id           bigserial PRIMARY KEY,
    src_file_id  bigint NOT NULL REFERENCES src_file (id) ON DELETE CASCADE,
    inn          text NOT NULL,
    standard     text NOT NULL DEFAULT 'rsbu' CHECK (standard IN ('rsbu', 'ifrs')),
    report_date  date NOT NULL,
    form_code    text NOT NULL,
    line_code    text NOT NULL,
    source_line_code text NOT NULL,
    value        numeric(20, 3),
    value_status text NOT NULL DEFAULT 'ok'
                 CHECK (value_status IN ('ok', 'not_disclosed', 'not_applicable')),
    period_role  text NOT NULL
                 CHECK (period_role IN ('current', 'previous', 'before_previous')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fact_report_uniq UNIQUE (inn, standard, report_date, form_code, line_code),
    -- Значение есть тогда и только тогда, когда показатель раскрыт.
    CONSTRAINT fact_report_value_status_consistent
        CHECK ((value IS NOT NULL) = (value_status = 'ok'))
);

CREATE INDEX IF NOT EXISTS fact_report_period_idx ON fact_report (inn, report_date);
CREATE INDEX IF NOT EXISTS fact_report_line_idx ON fact_report (line_code);
CREATE INDEX IF NOT EXISTS fact_report_src_idx ON fact_report (src_file_id);

COMMENT ON TABLE fact_report IS 'Одна строка — один код показателя за один период по одной организации';
COMMENT ON COLUMN fact_report.value IS 'Тысячи рублей; NULL — показатель не раскрыт, замена нулём запрещена';
COMMENT ON COLUMN fact_report.report_date IS 'Дата, на которую (или за период до которой) приведено значение';
COMMENT ON COLUMN fact_report.line_code IS
    'Канонический код строки из methodology/lines.yaml. Для упрощённых форм код укрупнённой строки '
    'в отчётности берётся по показателю с наибольшим удельным весом и между периодами меняется, '
    'поэтому ключом служит канонический код, а не код источника';
COMMENT ON COLUMN fact_report.period_role IS
    'Каким периодом значение пришло в комплекте: current — отчётный, previous — сравнительный, '
    'before_previous — позапрошлый (есть только в балансе). Сравнительное значение не затирает '
    'уже загруженное отчётное, иначе результат зависел бы от порядка загрузки комплектов';
COMMENT ON COLUMN fact_report.source_line_code IS
    'Код строки, фактически указанный в отчётности. Заполняется всегда: для полных форм '
    'совпадает с line_code, для упрощённых может отличаться. NULL не используется, '
    'чтобы join и сравнения не требовали COALESCE';
COMMENT ON COLUMN fact_report.value_status IS
    'ok — значение раскрыто; not_disclosed — прочерк, «X» или пустая ячейка; '
    'not_applicable — строка неприменима к данной форме отчётности организации';

-- Журнал контролей качества --------------------------------------------------

CREATE TABLE IF NOT EXISTS dq_log (
    id             bigserial PRIMARY KEY,
    src_file_id    bigint REFERENCES src_file (id) ON DELETE CASCADE,
    inn            text NOT NULL,
    report_date    date,
    form_code      text,
    line_code      text,
    check_code     text NOT NULL,
    status         text NOT NULL CHECK (status IN ('pass', 'fail', 'warning', 'info')),
    severity       text NOT NULL CHECK (severity IN ('blocking', 'warning', 'info')),
    message        text,
    previous_value numeric(20, 3),
    new_value      numeric(20, 3),
    details        jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dq_log_src_idx ON dq_log (src_file_id);
CREATE INDEX IF NOT EXISTS dq_log_inn_idx ON dq_log (inn, report_date);
CREATE INDEX IF NOT EXISTS dq_log_check_idx ON dq_log (check_code);

COMMENT ON TABLE dq_log IS 'Результаты контролей качества; провал блокирующего контроля отправляет src_file в карантин';
COMMENT ON COLUMN dq_log.details IS 'Фактические значения, участвовавшие в контроле, с кодами строк';
COMMENT ON COLUMN dq_log.check_code IS
    'Код контроля; служебный код fact_overwrite фиксирует перезаписи строки fact_report при повторной загрузке';
COMMENT ON COLUMN dq_log.previous_value IS 'Прежнее значение строки fact_report до перезаписи, в тысячах рублей';
COMMENT ON COLUMN dq_log.new_value IS 'Новое значение строки fact_report после перезаписи, в тысячах рублей';
COMMENT ON COLUMN dq_log.status IS 'info — запись информационная (например, перезапись значения), карантин не вызывает';

-- Рассчитанные показатели ----------------------------------------------------

CREATE TABLE IF NOT EXISTS metric_value (
    id                    bigserial PRIMARY KEY,
    inn                   text NOT NULL,
    standard              text NOT NULL DEFAULT 'rsbu' CHECK (standard IN ('rsbu', 'ifrs')),
    report_date           date NOT NULL,
    metric_code           text NOT NULL,
    value                 numeric(30, 10),
    status                text NOT NULL CHECK (status IN ('ok', 'not_calculable')),
    confidence            text NOT NULL DEFAULT 'verified'
                          CHECK (confidence IN ('verified', 'comparative_only', 'quarantined')),
    reason                text,
    reason_code           text,
    methodology_version   text NOT NULL,
    computed_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT metric_value_uniq UNIQUE (inn, standard, report_date, metric_code)
);

COMMENT ON COLUMN metric_value.standard IS
    'Стандарт отчётности, по которому посчитан показатель. Входит в ключ: ряды по РСБУ '
    'и по МСФО смешивать нельзя, а без этого поля расчёт по одному стандарту затирал бы другой';

CREATE INDEX IF NOT EXISTS metric_value_period_idx ON metric_value (inn, report_date);

COMMENT ON TABLE metric_value IS 'Значения коэффициентов; считает Python, не языковая модель';
COMMENT ON COLUMN metric_value.confidence IS
    'Доверие к периоду: verified — период проверен блокирующими контролями в своём комплекте; '
    'comparative_only — период пришёл только сравнительной колонкой и блокирующими контролями '
    'не проверялся; quarantined — собственный комплект периода в карантине';
COMMENT ON COLUMN metric_value.status IS 'not_calculable — нет входных данных, подстановка приближений запрещена';
COMMENT ON COLUMN metric_value.reason IS 'Причина нерасчёта с указанием отсутствующего кода строки';

-- Оценка финансового состояния -----------------------------------------------

-- Класс без разложения защитить нельзя: на вопрос «почему класс такой» нужно
-- отвечать запросом, а не пересчётом. Поэтому четыре таблицы, а не одна.

CREATE TABLE IF NOT EXISTS assessment (
    id                  bigserial PRIMARY KEY,
    inn                 text NOT NULL,
    standard            text NOT NULL DEFAULT 'rsbu' CHECK (standard IN ('rsbu', 'ifrs')),
    report_date         date NOT NULL,
    total_score         numeric(6, 2),
    -- Класса может не быть: основание оценки бывает слишком узким.
    class_code          text,
    class_name          text,
    no_class_reason     text,
    -- Почему балльная оценка не формируется: основание слишком узкое.
    -- Заполняется независимо от класса: при сработавшем стоп-факторе класс
    -- присвоен, а балльной оценки всё равно нет.
    breadth_reason      text,
    class_before_stop   text,
    stop_factor_code    text,
    stop_factor_effect  text CHECK (stop_factor_effect IN ('none', 'lowest_class', 'cap_at_class')),
    confidence          text NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    confidence_reasons  jsonb,
    metrics_version     text NOT NULL,
    scoring_version     text NOT NULL,
    flags_version       text NOT NULL,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT assessment_uniq UNIQUE (inn, standard, report_date),
    -- Либо класс присвоен, либо названа причина, по которой он не присвоен.
    -- Молчаливого отсутствия класса быть не может.
    CONSTRAINT assessment_class_or_reason
        CHECK ((class_code IS NOT NULL) <> (no_class_reason IS NOT NULL))
);

COMMENT ON TABLE assessment IS 'Класс финансового состояния; арифметика фиксирована методикой, модель его не определяет';
COMMENT ON COLUMN assessment.class_before_stop IS 'Класс по баллу до применения стоп-фактора: видно, что именно изменил стоп-фактор';
COMMENT ON COLUMN assessment.no_class_reason IS
    'Почему класс не присвоен: основание оценки слишком узкое. Главный случай — одна группа '
    'показателей забирает больше половины веса, и класс становится функцией этой группы';
COMMENT ON COLUMN assessment.confidence IS 'Уверенность в оценке; считается отдельно от класса и на него не влияет';

CREATE TABLE IF NOT EXISTS assessment_group (
    id               bigserial PRIMARY KEY,
    assessment_id    bigint NOT NULL REFERENCES assessment (id) ON DELETE CASCADE,
    group_code       text NOT NULL,
    group_name       text,
    score            numeric(6, 2),
    nominal_weight   numeric(6, 4) NOT NULL,
    effective_weight numeric(6, 4) NOT NULL,
    metrics_used     integer NOT NULL DEFAULT 0,
    metrics_excluded integer NOT NULL DEFAULT 0,
    CONSTRAINT assessment_group_uniq UNIQUE (assessment_id, group_code)
);

COMMENT ON COLUMN assessment_group.effective_weight IS
    'Вес после исключения групп без единого рассчитанного показателя и нормировки остальных';

CREATE TABLE IF NOT EXISTS assessment_metric (
    id               bigserial PRIMARY KEY,
    assessment_id    bigint NOT NULL REFERENCES assessment (id) ON DELETE CASCADE,
    metric_code      text NOT NULL,
    group_code       text NOT NULL,
    value            numeric(30, 10),
    score            numeric(6, 2),
    level_score      numeric(6, 2),
    dynamics_score   numeric(6, 2),
    periods_used     integer NOT NULL DEFAULT 0,
    included         boolean NOT NULL,
    exclusion_reason text,
    -- Машинный вид причины исключения: по нему причины упорядочиваются
    -- по фиксированной иерархии, а текст остаётся пояснением.
    exclusion_kind   text CHECK (exclusion_kind IN
                     ('stop_factor', 'no_level_scale', 'duplicate', 'no_data')),
    CONSTRAINT assessment_metric_uniq UNIQUE (assessment_id, metric_code)
);

COMMENT ON COLUMN assessment_metric.level_score IS
    'Балл за положение относительно бесспорного ориентира; NULL — ориентира нет, балл строится на динамике';
COMMENT ON COLUMN assessment_metric.exclusion_reason IS
    'Почему показатель не участвовал: не рассчитан, недостаточно периодов, неприменим к форме';

CREATE TABLE IF NOT EXISTS assessment_flag (
    id            bigserial PRIMARY KEY,
    assessment_id bigint NOT NULL REFERENCES assessment (id) ON DELETE CASCADE,
    flag_code     text NOT NULL,
    flag_name     text,
    level         text NOT NULL,
    affects_class boolean NOT NULL DEFAULT false,
    message       text NOT NULL,
    details       jsonb,
    CONSTRAINT assessment_flag_uniq UNIQUE (assessment_id, flag_code)
);

COMMENT ON TABLE assessment_flag IS
    'Сработавшие флаги с готовым текстом оговорки для раздела «Ограничения анализа»';

CREATE TABLE IF NOT EXISTS assessment_signal (
    id            bigserial PRIMARY KEY,
    assessment_id bigint NOT NULL REFERENCES assessment (id) ON DELETE CASCADE,
    signal_code   text NOT NULL,
    signal_name   text,
    level         text NOT NULL CHECK (level IN ('attention', 'supervisory')),
    value         numeric(30, 10),
    message       text NOT NULL,
    details       jsonb,
    CONSTRAINT assessment_signal_uniq UNIQUE (assessment_id, signal_code)
);

COMMENT ON TABLE assessment_signal IS
    'Сработавшие надзорные сигналы с предписанной формулировкой для раздела '
    '«Риски и надзорные сигналы». Сигнал — арифметика, а не интерпретация: '
    'условие проверяется по формуле, формулировка берётся из methodology/signals.yaml';

-- Журнал обращений к языковой модели -----------------------------------------

CREATE TABLE IF NOT EXISTS llm_log (
    id              bigserial PRIMARY KEY,
    inn             text,
    report_date     date,
    model           text NOT NULL,
    prompt_name     text,
    prompt_text     text,
    response_text   text,
    temperature     numeric,
    verified        boolean,
    foreign_numbers jsonb,
    attempt         integer NOT NULL DEFAULT 1,
    duration_ms     integer,
    -- Запись сделана тестом, а не рабочим прогоном. Журнал обращений
    -- к модели — доказательная база системы, и стирать его прогоном тестов
    -- нельзя. Тесты помечают свои записи и убирают только их.
    is_test         boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_log_real_idx ON llm_log (inn, created_at)
    WHERE NOT is_test;

CREATE INDEX IF NOT EXISTS llm_log_inn_idx ON llm_log (inn, report_date);

COMMENT ON TABLE llm_log IS 'Каждое обращение к модели с результатом постпроверки';
COMMENT ON COLUMN llm_log.verified IS 'false — ответ содержит посторонние числа и пользователю не показывается';
COMMENT ON COLUMN llm_log.foreign_numbers IS 'Числа из ответа, не найденные во входных блоках';

-- Доверие к периоду ----------------------------------------------------------

-- Один и тот же период приходит и своим комплектом, и сравнительной колонкой
-- более поздних. Блокирующие контроли применяются только к отчётному периоду
-- комплекта, поэтому период, существующий ТОЛЬКО сравнительной колонкой,
-- ими не проверялся. Без явного признака динамика за три года выглядела бы
-- одинаково достоверной.
CREATE OR REPLACE VIEW period_quality AS
SELECT
    f.inn,
    f.standard,
    f.report_date,
    bool_or(f.period_role = 'current')                                  AS has_own_report,
    bool_or(f.period_role = 'current' AND s.status = 'quarantine')      AS own_report_quarantined,
    count(*)                                                            AS lines_total,
    count(f.value)                                                      AS lines_disclosed,
    min(f.src_file_id) FILTER (WHERE f.period_role = 'current')         AS own_src_file_id,
    CASE
        WHEN bool_or(f.period_role = 'current' AND s.status = 'quarantine') THEN 'quarantined'
        WHEN NOT bool_or(f.period_role = 'current') THEN 'comparative_only'
        ELSE 'verified'
    END                                                                 AS confidence
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
GROUP BY f.inn, f.standard, f.report_date;

COMMENT ON VIEW period_quality IS
    'Доверие к периоду: проверялся ли он блокирующими контролями в собственном комплекте';

COMMIT;
