-- Схема базы findb. Применение: psql findb -f sql/001_schema.sql
-- Файл идемпотентен: повторное выполнение не меняет состояние.
-- Денежные величины — только numeric. NULL означает «не раскрыто», не ноль.
--
-- ВАЖНО О ПОВТОРНОМ ПРИМЕНЕНИИ. CREATE TABLE IF NOT EXISTS не приносит
-- в существующую таблицу колонки и ограничения, добавленные позже её
-- создания: он видит таблицу и целиком пропускает объявление. Поэтому
-- **каждое изменение существующей таблицы сопровождается догонкой** —
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS либо DO-блоком для ограничения,
-- в том же коммите, что и правка объявления. Трижды забывали: колонка
-- llm_log.code_version, внешние ключи разложения оценки, src_file.digit_grouping.
--
-- Колонки, добавленные до появления этого правила, догонок не имеют: часть
-- из них объявлена NOT NULL без значения по умолчанию, и дописать их задним
-- числом в базу с данными нельзя — значение взять неоткуда. Для таких баз
-- путь один: пересоздание (make db-reset). Расхождение базы с файлом ловит
-- сверка схемы (finlib/schema.py) на первом же этапе цикла.

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
    -- Конвенция записи чисел в исходном документе. Для источников, отдающих
    -- числа машиночитаемо (ГИР БО), не определяется и остаётся NULL: там
    -- разделителей разрядов нет вовсе. Для файла МСФО определяется по всему
    -- документу, и неопределённость означает карантин, а не выбор по умолчанию.
    digit_grouping    text CHECK (digit_grouping IN ('russian', 'english', 'plain')),
    -- Вид отчётности по МСФО. Отдельно от reporting_type: тот описывает набор
    -- форм РСБУ (полные или упрощённые по приложению 5 к приказу 66н)
    -- и к консолидированной отчётности отношения не имеет. NULL у комплектов
    -- РСБУ означает именно это — понятие к ним неприменимо.
    reporting_kind    text CHECK (reporting_kind IN
                      ('full', 'interim', 'special_purpose', 'disclosable')),
    status            text NOT NULL DEFAULT 'loaded'
                      CHECK (status IN ('loaded', 'processed', 'quarantine')),
    quarantine_reason text,
    meta              jsonb,
    loaded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT src_file_uniq UNIQUE (inn, standard, report_year, source, correction_version)
);

-- Колонка заведена позже таблицы: CREATE TABLE IF NOT EXISTS её в готовую
-- базу не принесёт. NULL означает «не определялась», и для источников,
-- отдающих числа машиночитаемо, это верно — разделителей разрядов там нет.
ALTER TABLE src_file ADD COLUMN IF NOT EXISTS digit_grouping text;
ALTER TABLE src_file ADD COLUMN IF NOT EXISTS reporting_kind text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'src_file'::regclass AND conname = 'src_file_digit_grouping_check'
    ) THEN
        ALTER TABLE src_file ADD CONSTRAINT src_file_digit_grouping_check
            CHECK (digit_grouping IN ('russian', 'english', 'plain'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'src_file'::regclass AND conname = 'src_file_reporting_kind_check'
    ) THEN
        ALTER TABLE src_file ADD CONSTRAINT src_file_reporting_kind_check
            CHECK (reporting_kind IN
                   ('full', 'interim', 'special_purpose', 'disclosable'));
    END IF;
END $$;

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
COMMENT ON COLUMN src_file.digit_grouping IS
    'Конвенция записи чисел исходного документа: russian — разряды пробелом, '
    'english — разряды запятой, plain — разделителей нет. NULL — не определялась '
    '(источник отдаёт числа машиночитаемо). Неверная конвенция не ловится ни одним '
    'контролем сходимости: баланс сойдётся, а все абсолютные величины будут '
    'неверны в тысячу раз';
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
    -- Чем строка опознана. Две силы опознания, и доверие к ним разное:
    -- catalog — справочник утверждает о строке с таким наименованием вообще;
    -- confirmation — человек сказал, чем эта строка является **у этого
    -- эмитента**. Величины участвуют в расчёте наравне, а в документе
    -- печатаются порознь. Без этой графы подтверждённые статьи в факты
    -- не писались вовсе: у Норникеля из расчёта выпадали все 64
    -- подтверждённые статьи, у Автодора — 39 из 40, включая две,
    -- в которых лежат 85 % активов.
    recognition  text NOT NULL DEFAULT 'catalog'
                 CHECK (recognition IN ('catalog', 'confirmation', 'note')),
    -- Откуда величина взята, когда она из примечания: номер примечания
    -- и наименования его строк. Читатель, сверяющий заключение с отчётностью,
    -- обязан видеть источник: у Автодора в строке формы 414, а начислено
    -- по примечанию 54 382, и без ссылки расхождение выглядит ошибкой расчёта.
    note_number  integer,
    note_source_name text,
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
-- Графа заведена 21.09.2026; правило догонки действует с 17.09.2026.
ALTER TABLE fact_report
    ADD COLUMN IF NOT EXISTS recognition text NOT NULL DEFAULT 'catalog';
ALTER TABLE fact_report DROP CONSTRAINT IF EXISTS fact_report_recognition_check;
ALTER TABLE fact_report
    ADD CONSTRAINT fact_report_recognition_check
    CHECK (recognition IN ('catalog', 'confirmation', 'note'));
-- Ссылка на примечание заведена 21.09.2026 вместе с третьей силой опознания.
ALTER TABLE fact_report ADD COLUMN IF NOT EXISTS note_number integer;
ALTER TABLE fact_report ADD COLUMN IF NOT EXISTS note_source_name text;

COMMENT ON COLUMN fact_report.recognition IS
    'catalog — строка опознана справочником, confirmation — принята по коду, '
    'присвоенному человеком у этого же эмитента, note — величина взята '
    'из примечания по ссылке из строки формы. Доверие разное, участие '
    'в расчёте одинаковое';
COMMENT ON COLUMN fact_report.note_number IS
    'Номер примечания, из которого взята величина; NULL — величина не '
    'из примечания';
COMMENT ON COLUMN fact_report.note_source_name IS
    'Наименования строк примечания, давших величину, — дословно';

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
    -- Стоп-фактор, назначивший ограничение класса: при нескольких
    -- сработавших это тот, чьё ограничение младше.
    stop_factor_code    text,
    -- **Все сработавшие стоп-факторы, а не только назначивший класс.**
    -- У Сегежи сработали три — неопределённость непрерывности, отрицательный
    -- оборотный капитал и покрытие процентов ниже единицы, — а документ
    -- называл один: два обстоятельства из трёх читателю не доходили вовсе.
    -- Ограничение ниже не даёт полям разойтись: назначивший класс обязан
    -- стоять среди сработавших.
    stop_factor_codes   jsonb,
    stop_factor_effect  text CHECK (stop_factor_effect IN ('none', 'lowest_class', 'cap_at_class')),
    -- Сверка сработавшего стоп-фактора с аудиторским заключением: согласуется
    -- ли он с разделом о непрерывности деятельности. Стоп-фактор с внешним
    -- подтверждением и без него равно остаются в силе, но формулировки во
    -- втором случае обязаны быть осторожнее, и нечитаемое заключение — третий
    -- исход, а не второй.
    stop_factor_audit   text,
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
        CHECK ((class_code IS NOT NULL) <> (no_class_reason IS NOT NULL)),
    -- Стоп-фактор, назначивший класс, обязан стоять среди сработавших: два
    -- поля одной величины умеют разойтись, и расхождение здесь означало бы
    -- класс, ограниченный стоп-фактором, которого не было.
    CONSTRAINT assessment_stop_factor_listed
        CHECK (stop_factor_code IS NULL OR stop_factor_codes ? stop_factor_code)
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
                     ('stop_factor', 'no_level_scale', 'duplicate',
                      'not_routing', 'no_data')),
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

-- Связь разложения оценки с самой оценкой. В объявлениях таблиц выше ключ
-- стоит, но в базах, созданных до его появления, CREATE TABLE IF NOT EXISTS
-- его не принесёт — та же история, что с колонкой code_version. Связь здесь
-- не формальность: стандарт отчётности разложение получает от оценки
-- и вторым полем не дублируется, а без внешнего ключа это наследование
-- ничем не обеспечено.
-- Имя ограничения — то же, какое Postgres даёт ключу, объявленному в CREATE
-- TABLE: иначе база и файл разойдутся именем при совпадающем смысле, а сверка
-- схемы сравнивает их буквально. Ключ, созданный прежней редакцией этой
-- догонки под другим именем, переименовывается, а не дублируется.
DO $$
DECLARE
    part text;
    canonical text;
    existing text;
BEGIN
    FOREACH part IN ARRAY ARRAY[
        'assessment_metric', 'assessment_group', 'assessment_flag', 'assessment_signal'
    ] LOOP
        canonical := part || '_assessment_id_fkey';
        SELECT conname INTO existing FROM pg_constraint
        WHERE conrelid = part::regclass AND contype = 'f';

        IF existing IS NULL THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I '
                'FOREIGN KEY (assessment_id) REFERENCES assessment (id) ON DELETE CASCADE',
                part, canonical
            );
        ELSIF existing <> canonical THEN
            EXECUTE format(
                'ALTER TABLE %I RENAME CONSTRAINT %I TO %I', part, existing, canonical
            );
        END IF;
    END LOOP;
END $$;

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
    -- Сколько пар «число — код» постпроверка сверила. Без этой величины
    -- ноль посторонних чисел и отсутствие чисел в ответе — одна и та же
    -- запись, а статистика отказов считается по журналу.
    checked_numbers integer,
    attempt         integer NOT NULL DEFAULT 1,
    duration_ms     integer,
    -- Версия кода, которой сделан прогон: короткий git-хеш рабочего дерева.
    -- Записи разных версий несопоставимы: правка инструкции или постпроверки
    -- меняет поведение текстового слоя целиком, и статистика по смеси версий
    -- описывает историю разработки, а не систему.
    code_version    text,
    -- Запись сделана тестом, а не рабочим прогоном. Журнал обращений
    -- к модели — доказательная база системы, и стирать его прогоном тестов
    -- нельзя. Тесты помечают свои записи и убирают только их.
    is_test         boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Колонка добавлена после того, как таблица уже существовала в рабочей базе.
-- CREATE TABLE IF NOT EXISTS её туда не принесёт, а журнал пересоздавать
-- нельзя: он доказательная база системы. Прежние записи остаются с NULL —
-- версия тех прогонов действительно неизвестна.
ALTER TABLE llm_log ADD COLUMN IF NOT EXISTS code_version text;

-- Та же причина: колонка заведена позже таблицы. NULL в прежних записях
-- означает именно то, что там написано, — сколько чисел сверено, неизвестно.
ALTER TABLE llm_log ADD COLUMN IF NOT EXISTS checked_numbers integer;

CREATE INDEX IF NOT EXISTS llm_log_real_idx ON llm_log (inn, created_at)
    WHERE NOT is_test;

CREATE INDEX IF NOT EXISTS llm_log_inn_idx ON llm_log (inn, report_date);

COMMENT ON TABLE llm_log IS 'Каждое обращение к модели с результатом постпроверки';
COMMENT ON COLUMN llm_log.verified IS 'false — ответ содержит посторонние числа и пользователю не показывается';
COMMENT ON COLUMN llm_log.foreign_numbers IS 'Числа из ответа, не найденные во входных блоках';
COMMENT ON COLUMN llm_log.checked_numbers IS
    'Сколько пар «число — код» сверено; NULL — постпроверка не выполнялась. '
    'Ноль нарушений при неизвестном числе проверок — не успех, а отсутствие сведений';
COMMENT ON COLUMN llm_log.code_version IS 'Версия кода прогона (git-хеш); записи других версий в статистику не идут';

-- Подтверждённые специфические статьи МСФО ------------------------------------

-- Статья консолидированной отчётности, превышающая порог существенности,
-- никогда не сворачивается в «прочее»: она выносится отдельной позицией
-- с кодом, присвоенным человеком на экране сверки. Такие коды живут здесь,
-- а не в methodology/ifrs_lines.yaml: методика правится руками и диффом,
-- а позиция, присвоенная во время работы, методикой не является.
--
-- Наименование хранится ДОСЛОВНО, как оно стояло в отчётности. Через полгода
-- при решении, поднимать ли позицию в ядро, нужно видеть, одну ли вещь
-- подтверждали у разных эмитентов под разными названиями, — по коду этого
-- не увидеть, код присваивали мы.
CREATE TABLE IF NOT EXISTS ifrs_line_confirmation (
    id             bigserial PRIMARY KEY,
    code           text NOT NULL CHECK (code ~ '^ifrs\.[a-z][a-z0-9_]*$'),
    inn            text NOT NULL REFERENCES organization (inn) ON DELETE CASCADE,
    src_file_id    bigint REFERENCES src_file (id) ON DELETE SET NULL,
    report_date    date NOT NULL,
    -- Наименование статьи в отчётности эмитента, без нормализации. Запись
    -- о том, что было: не правится никогда, даже если разбор прочитал
    -- наименование с мусором — «Поступление от выпуска акций 19 51 012 -».
    source_name    text NOT NULL,
    -- Ключ сопоставления, вычисленный **текущим разбором** из наименования.
    -- Величина производная, поэтому пересчитывается при каждом присесте
    -- разметки: когда разбор научится отрезать номер примечания, прежние
    -- подтверждения начнут находиться, а `source_name` останется как был.
    -- NULL — ключ ещё не вычислялся, и тогда сопоставление идёт по
    -- наименованию, как прежде.
    match_key      text,
    -- Раздел отчётности и величина, ради которой статья вынесена отдельно.
    form_code      text NOT NULL,
    value          numeric(20, 3),
    -- Мера существенности: величина строки к базе **своей формы**. База
    -- объявлена методикой (`materiality.bases`): баланс мерится валютой
    -- баланса, отчёт о прибыли — выручкой, у отчёта о движении денежных
    -- средств базы нет вовсе, и тогда здесь NULL. NULL означает «мерить
    -- нечем», а не «несущественна»: ноль означал бы второе.
    --
    -- Прежде графа называлась share_of_assets и у строки отчёта о прибыли
    -- содержала долю выручки, а у строки потока — отношение оборота за год
    -- к запасу на дату: у О'КЕЙ 336,9 % валюты баланса.
    materiality_share numeric(10, 6),
    -- По какому правилу посчитана мера. Журнал — доказательная база, и задним
    -- числом он не правится: 290 записей, сделанных до 21.09.2026, хранят долю
    -- валюты баланса у строк любой формы — правило, которое тогда действовало.
    -- Переписать их значило бы подменить запись о том, что было; поэтому
    -- правило названо рядом с величиной.
    materiality_rule text NOT NULL DEFAULT 'per_form_base'
                 CHECK (materiality_rule IN ('total_assets', 'per_form_base')),
    -- Вид разметки: чем строка приходится позиции справочника. От него
    -- зависит, как разметка проверяется арифметикой, и смешивать виды
    -- нельзя. exact — строка и есть позиция; part_of — строка вместе
    -- с соседними даёт позицию, и сумма таких строк обязана ей равняться;
    -- aggregate_of — строка укрупняет несколько позиций, перечень
    -- в related_codes; specific — содержание не укладывается ни в одну
    -- позицию и не раскладывается на существующие.
    relation       text NOT NULL DEFAULT 'exact'
                   CHECK (relation IN ('exact', 'part_of', 'aggregate_of', 'specific')),
    -- Место строки в таблице формы. Наименование ключом быть не может:
    -- у части строк его нет вовсе, а «Прочие расходы» встречаются в форме
    -- дважды — разметка применялась не к той строке либо не применялась.
    row_index      integer,
    related_codes  text[],
    -- Подтвердилась ли разметка арифметикой: сумма сошлась с величиной
    -- позиции. NULL — проверить было нечем, и это не то же самое, что
    -- «не сошлось».
    arithmetic_confirmed boolean,
    confirmed_by   text NOT NULL,
    confirmed_at   timestamptz NOT NULL DEFAULT now(),
    note           text,
    -- **Одна строка комплекта — одно решение человека.** Ключ уникальности
    -- строится по строке, а не по паре «строка, код»: иначе исправление
    -- не заменяет прежнее решение, а добавляется рядом. У ФосАгро строка
    -- «права пользования» так получила три кода — балансовый от 18.09,
    -- детализацию и специфический от 20.09, — и разметка не применялась
    -- вовсе: прежнее притязание отклонялось правилом формы и возвращало
    -- строку в очередь. Человек размечал её три присеста подряд.
    CONSTRAINT ifrs_line_confirmation_row_uniq
        UNIQUE (inn, report_date, form_code, row_index)
);

-- Колонки заведены позже таблицы; правило догонки действует с 17.09.2026.
ALTER TABLE ifrs_line_confirmation
    ADD COLUMN IF NOT EXISTS relation text NOT NULL DEFAULT 'exact';
ALTER TABLE ifrs_line_confirmation ADD COLUMN IF NOT EXISTS related_codes text[];
ALTER TABLE ifrs_line_confirmation
    ADD COLUMN IF NOT EXISTS arithmetic_confirmed boolean;
ALTER TABLE ifrs_line_confirmation ADD COLUMN IF NOT EXISTS row_index integer;

-- Графа share_of_assets 21.09.2026 переименована в materiality_share и стала
-- допускать NULL: мера считается от базы своей формы, а у отчёта о движении
-- денежных средств базы нет вовсе. Переименование, а не новая графа: величины
-- балансовых строк в ней верны, и терять их незачем. Догонка идёт блоком,
-- потому что RENAME COLUMN не знает IF EXISTS.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ifrs_line_confirmation' AND column_name = 'share_of_assets'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ifrs_line_confirmation' AND column_name = 'materiality_share'
    ) THEN
        ALTER TABLE ifrs_line_confirmation
            RENAME COLUMN share_of_assets TO materiality_share;
    END IF;
END $$;
ALTER TABLE ifrs_line_confirmation
    ADD COLUMN IF NOT EXISTS materiality_share numeric(10, 6);
ALTER TABLE ifrs_line_confirmation
    ALTER COLUMN materiality_share DROP NOT NULL;

-- Правило, по которому посчитана мера, называется рядом с ней. Догонка
-- проставляет прежним записям прежнее правило и лишь потом объявляет
-- умолчание: `ADD COLUMN ... DEFAULT` присвоил бы им нынешнее, то есть
-- сказал бы о них неправду.
-- Ключ сопоставления заведён 21.09.2026. Значение вычисляет Python (правило
-- приведения наименования одно на весь проект), поэтому догонка добавляет
-- только графу: заполняет её присест разметки.
ALTER TABLE ifrs_line_confirmation ADD COLUMN IF NOT EXISTS match_key text;
CREATE INDEX IF NOT EXISTS ifrs_line_confirmation_match_idx
    ON ifrs_line_confirmation (inn, form_code, match_key);

ALTER TABLE ifrs_line_confirmation ADD COLUMN IF NOT EXISTS materiality_rule text;
UPDATE ifrs_line_confirmation SET materiality_rule = 'total_assets'
    WHERE materiality_rule IS NULL;
ALTER TABLE ifrs_line_confirmation
    ALTER COLUMN materiality_rule SET DEFAULT 'per_form_base';
ALTER TABLE ifrs_line_confirmation
    ALTER COLUMN materiality_rule SET NOT NULL;
ALTER TABLE ifrs_line_confirmation
    DROP CONSTRAINT IF EXISTS ifrs_line_confirmation_materiality_rule_check;
ALTER TABLE ifrs_line_confirmation
    ADD CONSTRAINT ifrs_line_confirmation_materiality_rule_check
    CHECK (materiality_rule IN ('total_assets', 'per_form_base'));

-- Перечень видов разметки 18.09.2026 пополнился решением «не статья»:
-- прежде оно жило один присест и в базу не попадало вовсе, поэтому строка
-- возвращалась в очередь и размечалась заново — иногда иначе. Ограничение
-- снимается и ставится заново, а не добавляется при отсутствии: иначе
-- в базе, заведённой раньше, остался бы прежний перечень, и догонка
-- не состоялась бы.
ALTER TABLE ifrs_line_confirmation
    DROP CONSTRAINT IF EXISTS ifrs_line_confirmation_relation_check;
ALTER TABLE ifrs_line_confirmation
    ADD CONSTRAINT ifrs_line_confirmation_relation_check
    CHECK (relation IN ('exact', 'part_of', 'aggregate_of', 'specific', 'not_a_line'));

-- Ключ уникальности заменён 21.09.2026: был по паре «код, строка», стал
-- по строке. Прежний позволял держать у одной строки несколько решений
-- с разными кодами, и исправление не заменяло ошибку, а ложилось рядом:
-- восстановление применяло все сразу, а отклонённое правилом формы
-- возвращало строку в очередь молча. Догонка обязательна — в рабочей базе
-- ограничение уже стоит, и `CREATE TABLE IF NOT EXISTS` его не тронет.
--
-- Дубли снимаются до постановки ограничения, и снимается **старое**:
-- последнее решение человека и есть его решение, а прежние — исправленные
-- ошибки. Строки без индекса (записи прежних сессий) ограничением
-- не охватываются: NULL уникальности не нарушает, и трогать историю
-- ради формы незачем.
DELETE FROM ifrs_line_confirmation AS older
USING ifrs_line_confirmation AS newer
WHERE older.row_index IS NOT NULL
  AND older.inn = newer.inn
  AND older.report_date = newer.report_date
  AND older.form_code = newer.form_code
  AND older.row_index = newer.row_index
  AND (older.confirmed_at, older.id) < (newer.confirmed_at, newer.id);

ALTER TABLE ifrs_line_confirmation
    DROP CONSTRAINT IF EXISTS ifrs_line_confirmation_uniq;
ALTER TABLE ifrs_line_confirmation
    DROP CONSTRAINT IF EXISTS ifrs_line_confirmation_row_uniq;
ALTER TABLE ifrs_line_confirmation
    ADD CONSTRAINT ifrs_line_confirmation_row_uniq
    UNIQUE (inn, report_date, form_code, row_index);

CREATE INDEX IF NOT EXISTS ifrs_line_confirmation_code_idx
    ON ifrs_line_confirmation (code);

-- Сверка стоп-фактора с аудиторским заключением заведена 21.09.2026 вместе
-- с проводкой стоп-факторов ветки МСФО в расчёт по фактам. Догонка обязательна:
-- таблица в рабочей базе есть, и объявление колонки `CREATE TABLE IF NOT
-- EXISTS` в неё не принесёт. Значения прежним оценкам не проставляются —
-- по ним сверка не делалась, и приписывать им исход было бы неправдой.
ALTER TABLE assessment ADD COLUMN IF NOT EXISTS stop_factor_audit text;

-- Перечень всех сработавших стоп-факторов заведён 21.09.2026: документ называл
-- один — тот, что назначил класс, — и у Сегежи два обстоятельства из трёх
-- до читателя не доходили. Догонка обязательна, а прежним оценкам перечень
-- заполняется кодом назначившего стоп-фактора: он сработал наверняка, тогда
-- как об остальных прежняя запись не говорит ничего.
ALTER TABLE assessment ADD COLUMN IF NOT EXISTS stop_factor_codes jsonb;
UPDATE assessment
   SET stop_factor_codes = to_jsonb(ARRAY[stop_factor_code])
 WHERE stop_factor_code IS NOT NULL AND stop_factor_codes IS NULL;
ALTER TABLE assessment
    DROP CONSTRAINT IF EXISTS assessment_stop_factor_listed;
ALTER TABLE assessment
    ADD CONSTRAINT assessment_stop_factor_listed
    CHECK (stop_factor_code IS NULL OR stop_factor_codes ? stop_factor_code);

-- Вид причины `not_routing` заведён 21.09.2026 вместе с проводкой стоп-факторов
-- МСФО: показатель описывает деятельность, но решения не меняет — это наше
-- решение, а не пробел отчётности, и сводить его с «шкалы уровня нет» нельзя.
-- Ограничение заменяется целиком: `CREATE TABLE IF NOT EXISTS` в готовую
-- таблицу нового перечня не принесёт.
ALTER TABLE assessment_metric
    DROP CONSTRAINT IF EXISTS assessment_metric_exclusion_kind_check;
ALTER TABLE assessment_metric
    ADD CONSTRAINT assessment_metric_exclusion_kind_check
    CHECK (exclusion_kind IN
           ('stop_factor', 'no_level_scale', 'duplicate', 'not_routing', 'no_data'));

COMMENT ON TABLE ifrs_line_confirmation IS
    'Специфические статьи МСФО сверх порога существенности, подтверждённые '
    'человеком на экране сверки. Не методика: методика правится руками и диффом';
COMMENT ON COLUMN ifrs_line_confirmation.source_name IS
    'Наименование статьи дословно, как в отчётности эмитента. Нужно, чтобы '
    'увидеть, одну ли вещь подтверждали у разных эмитентов под разными названиями';
COMMENT ON COLUMN ifrs_line_confirmation.materiality_share IS
    'Мера существенности: величина строки к базе своей формы (баланс — валюта '
    'баланса, отчёт о прибыли — выручка). NULL — базы у формы нет, мерить нечем';

-- Кандидат в ядро: статья, подтверждённая у нескольких эмитентов независимо.
-- Признак машинный и никого ни к чему не обязывает — поднятие позиции в ядро
-- остаётся решением человека и правкой ifrs_lines.yaml руками. Признак лишь
-- показывает, что пора посмотреть. Порог (core_candidate.distinct_issuers)
-- задан методикой, здесь печатается само число эмитентов.
CREATE OR REPLACE VIEW ifrs_core_candidate AS
SELECT
    code,
    count(DISTINCT inn)                      AS issuers,
    count(*)                                 AS confirmations,
    array_agg(DISTINCT source_name ORDER BY source_name) AS source_names,
    max(materiality_share)                   AS max_share,
    max(confirmed_at)                        AS last_confirmed_at
FROM ifrs_line_confirmation
-- Решение «не статья» хранится здесь же, но статьёй не является и
-- кандидатом в ядро быть не может: иначе колонтитул, отмеченный у трёх
-- эмитентов, выглядел бы позицией, созревшей для справочника.
WHERE relation <> 'not_a_line'
GROUP BY code;

COMMENT ON VIEW ifrs_core_candidate IS
    'Специфические статьи в разрезе кода: сколько эмитентов подтвердили её '
    'независимо и под какими наименованиями. Порог кандидата в ядро задан '
    'в methodology/ifrs_lines.yaml, решение о поднятии принимает человек';

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
