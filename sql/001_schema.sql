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
    unit_source       text NOT NULL DEFAULT 'assumed'
                      CHECK (unit_source IN ('declared', 'assumed')),
    status            text NOT NULL DEFAULT 'loaded'
                      CHECK (status IN ('loaded', 'processed', 'quarantine')),
    quarantine_reason text,
    meta              jsonb,
    loaded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT src_file_uniq UNIQUE (inn, report_year, source, correction_version)
);

CREATE INDEX IF NOT EXISTS src_file_checksum_idx ON src_file (checksum);
CREATE INDEX IF NOT EXISTS src_file_status_idx ON src_file (status);

COMMENT ON TABLE src_file IS 'Загруженная отчётность как единица обработки';
COMMENT ON COLUMN src_file.checksum IS 'sha256 сырого ответа источника';
COMMENT ON COLUMN src_file.unit_code IS 'Единица измерения (ОКЕИ: 384 — тыс. руб., 385 — млн руб.)';
COMMENT ON COLUMN src_file.unit_source IS
    'declared — единица указана источником; assumed — принята по умолчанию. '
    'В ответе ГИР БО поля единицы измерения нет вообще, значения приходят в тысячах рублей, '
    'поэтому для него всегда assumed';
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

CREATE TABLE IF NOT EXISTS fact_report (
    id           bigserial PRIMARY KEY,
    src_file_id  bigint NOT NULL REFERENCES src_file (id) ON DELETE CASCADE,
    inn          text NOT NULL,
    report_date  date NOT NULL,
    form_code    text NOT NULL,
    line_code    text NOT NULL,
    source_line_code text NOT NULL,
    value        numeric(20, 3),
    value_status text NOT NULL DEFAULT 'ok'
                 CHECK (value_status IN ('ok', 'not_disclosed', 'not_applicable')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fact_report_uniq UNIQUE (inn, report_date, form_code, line_code),
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
    report_date           date NOT NULL,
    metric_code           text NOT NULL,
    value                 numeric(30, 10),
    status                text NOT NULL CHECK (status IN ('ok', 'not_calculable')),
    reason                text,
    methodology_version   text NOT NULL,
    computed_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT metric_value_uniq UNIQUE (inn, report_date, metric_code)
);

CREATE INDEX IF NOT EXISTS metric_value_period_idx ON metric_value (inn, report_date);

COMMENT ON TABLE metric_value IS 'Значения коэффициентов; считает Python, не языковая модель';
COMMENT ON COLUMN metric_value.status IS 'not_calculable — нет входных данных, подстановка приближений запрещена';
COMMENT ON COLUMN metric_value.reason IS 'Причина нерасчёта с указанием отсутствующего кода строки';

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
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_log_inn_idx ON llm_log (inn, report_date);

COMMENT ON TABLE llm_log IS 'Каждое обращение к модели с результатом постпроверки';
COMMENT ON COLUMN llm_log.verified IS 'false — ответ содержит посторонние числа и пользователю не показывается';
COMMENT ON COLUMN llm_log.foreign_numbers IS 'Числа из ответа, не найденные во входных блоках';

COMMIT;
