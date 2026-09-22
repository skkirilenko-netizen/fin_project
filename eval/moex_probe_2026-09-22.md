# Разведка ISS Московской биржи

Публичный доступ, без ключа. Ответы кладутся в `data/raw/moex/`, частота ограничена нами. **Правил из разведки не сделано.**

## Главное

1. **История торгов глубокая**: у выпуска ЕвроТранса 883 торговых дня с 04.04.2023 — то есть с размещения. Окна в сорок дней, как у агрегатора, здесь нет: рыночный слой можно строить на ISS и проверять на календаре событий назад.
2. **Все спрошенные поля есть**: доходность к погашению и к оферте, дюрация, объём, оборот, число сделок, цена закрытия и признаваемая котировка — и сверх того **Z-спред, посчитанный самой биржей**.
3. **Кривая бескупонной доходности ОФЗ есть** и с историей с 06.01.2014: параметры модели (B1–B3, T1, G1–G9), одиннадцать точек по срокам и корзина ОФЗ с доходностями и дюрациями.
4. **Индексы по рейтинговым группам есть**: RUCBCPAAANS, RUCBCPAANS, RUCBCPANS, RUCBCPBBBNS, отдельно ВДО (RUCBHYTR) и государственные (RGBITR с 30.12.2002, с полями доходности и дюрации).
5. **Перевод в сектор повышенного риска структурирован и датирован**: у ЕвроТранса режим TQCB кончается 05.08.2026, TQRD начинается 06.08.2026. Признаки `HIGHRISK`, `HASDEFAULT`, `HASTECHNICALDEFAULT` и `LISTLEVEL` стоят в карточке выпуска.
6. **Связь прямая**: SECID торгуемой облигации равен ISIN, и 816 выпусков списка наблюдения находятся в ISS; 16 из них — в секторе повышенного риска, и это ЕвроТранс, Антерра и Монополия, то есть те же три эмитента, которых событийный слой уже держит в разборе.

## 1. История торгов по облигациям

У выпуска `RU000A1061K1` (ЕвроТранс, БО-001Р-03) торговых дней **883**, первый — 2023-04-04. Ответ отдаётся страницами по 100, и общее число стоит в курсоре: глубина не обрезается окном, как у агрегатора.

**Поля дня** (всего 50), из спрошенных есть:

| Поле | Что это | Пример |
|---|---|---|
| `YIELDCLOSE` | доходность к погашению по цене закрытия | 13.43 |
| `YIELDATWAP` | доходность к погашению по средневзвешенной | 13.42 |
| `YIELDTOOFFER` | доходность к оферте | None |
| `DURATION` | дюрация, дней | 944 |
| `VOLUME` | объём в бумагах | 52786 |
| `VALUE` | оборот, руб. | 54079086.5 |
| `NUMTRADES` | число сделок | 1373 |
| `CLOSE` | цена закрытия, % номинала | 102.44 |
| `LEGALCLOSEPRICE` | признаваемая котировка | 102.44 |
| `MARKETPRICE3` | рыночная цена 3 | 102.45 |
| `ZSPREAD` | Z-спред к кривой ОФЗ, б. п. | None |
| `ACCINT` | накопленный купон | 8.2 |
| `COUPONPERCENT` | ставка купона | 13.6 |
| `OFFERDATE` | дата оферты | None |
| `MATDATE` | дата погашения | 2027-03-14 |
| `BOARDID` | режим торгов | TQCB |

**Z-спред уже посчитан биржей** (`ZSPREAD`), то есть спред к кривой ОФЗ считать самим не нужно — но он к кривой, а не к отдельной бумаге, и дюрация рядом стоит своя.

### Выпуски ЕвроТранса и Кириллицы, март–август 2026

| Эмитент | Выпуск | ISIN | Дней | Цена в марте | Цена в августе | Доходность в августе | Режим |
|---|---|---|---|---|---|---|---|
| ЕвроТранс | ЕвроТранс, БО-001P-01 | RU000A105PP9 | 0 | — | — | — | сделок нет |
| ЕвроТранс | ЕвроТранс, БО-001P-02 | RU000A105TS5 | 0 | — | — | — | сделок нет |
| ЕвроТранс | ЕвроТранс, БО-001P-03 | RU000A1061K1 | 133 | 89.17 (2026-03-02) | 12.96 (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, 002Р-01 | RU000A1082G5 | 133 | 71.84 (2026-03-02) | 7.94 (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, 002Р-02 | RU000A108D81 | 133 | 72.02 (2026-03-02) | 7.87 (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, 01 | RU000A109LH7 | 0 | — | — | — | сделок нет |
| ЕвроТранс | ЕвроТранс, БО-001Р-04 | RU000A10A133 | 133 | 100.9 (2026-03-02) | None (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, БО-001Р-05 | RU000A10A141 | 133 | 103.6 (2026-03-02) | 9.26 (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, БО-001Р-06 | RU000A10ATS0 | 133 | 103.84 (2026-03-02) | 9.12 (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, БО-001Р-07 | RU000A10BB75 | 133 | 100.94 (2026-03-02) | 10.35 (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, 01 | RU000A109LH7 | 0 | — | — | — | сделок нет |
| ЕвроТранс | ЕвроТранс, БО-001Р-08 | RU000A10CZB9 | 133 | 88 (2026-03-02) | 8.23 (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, 003Р-01 | RU000A10DEP2 | 0 | — | — | — | сделок нет |
| ЕвроТранс | ЕвроТранс, БО-001Р-09 | RU000A10E4X3 | 133 | 83.98 (2026-03-02) | 8.6 (2026-09-01) | None | TQCB → TQRD |
| ЕвроТранс | ЕвроТранс, 004Р-01 | RU000A10EC55 | 0 | — | — | — | сделок нет |
| Кириллица | Кириллица, БО-01 | RU000A104UY4 | 0 | — | — | — | сделок нет |
| Кириллица | Кириллица, БО-02 | RU000A106L67 | 94 | 95.62 (2026-03-02) | None (2026-07-08) | None | TQCB → TQCB |
| Кириллица | Кириллица, БО-03 | RU000A106UB7 | 126 | 94.79 (2026-03-02) | None (2026-08-21) | None | TQCB → TQCB |

## 2. Кривая бескупонной доходности ОФЗ

- сегодняшняя кривая (`engines/stock/zcyc.json`): блоки maxdates, params, params.dates, securities, securities.dates, yearyields, yearyields.dates
  - `maxdates`: строк 1, поля maxdate, months, tradedate
  - первая строка: {'tradedate': '2026-09-22', 'maxdate': '2047-06-23', 'months': 252}
  - `params`: строк 1, поля B1, B2, B3, G1, G2, G3, G4, G5, G6, G7, G8, G9, T1, tradedate, tradetime
  - первая строка: {'tradedate': '2026-09-22', 'tradetime': '16:27:56', 'B1': Decimal('1577.829030'), 'B2': Decimal('-439.177753'), 'B3': Decimal('-364.403397'), 'T1': Decimal('0.500053'), 'G1': Decimal('1.254496'), 'G2': Decimal('-8.981831'), 'G3': Decimal('4.143607'), 'G4': Decimal('5.435839'), 'G5': Decimal('2.877633'), 'G6': Decimal('0.878924'), 'G7': Decimal('0.311818'), 'G8': Decimal('0.000000'), 'G9': Decimal('0.000000')}
  - `params.dates`: строк 1, поля from, till
  - первая строка: {'from': '2014-01-06', 'till': '2026-09-22'}
  - `securities`: строк 26, поля askduration, askprice, askyield, benchmark, bidduration, bidprice, bidyield, clcprice, clcyield, correction, crtduration, crtprice, crtyield, expdate, secid, shortname, tradedate, tradetime, trdprice, trdyield, updatetime
  - первая строка: {'tradedate': '2026-09-22', 'tradetime': '16:27:56', 'secid': 'SU26207RMFS9', 'benchmark': 0, 'expdate': '2027-02-03', 'updatetime': '16:27:57', 'bidprice': Decimal('98.4200'), 'bidyield': Decimal('13.05'), 'askprice': Decimal('98.4410'), 'askyield': Decimal('12.99'), 'trdprice': Decimal('98.4390'), 'trdyield': Decimal('12.99'), 'clcprice': Decimal('99.6509'), 'crtprice': Decimal('98.4220'), 'clcyield': Decimal('12.53'), 'correction': Decimal('0.4618'), 'crtyield': Decimal('12.99'), 'crtduration': 134, 'bidduration': 133, 'askduration': 133, 'shortname': 'ОФЗ 26207'}
  - `securities.dates`: строк 1, поля from, till
  - первая строка: {'from': '2014-01-06', 'till': '2026-09-22'}
  - `yearyields`: строк 11, поля period, tradedate, tradetime, value
  - первая строка: {'tradedate': '2026-09-22', 'tradetime': '16:27:56', 'period': Decimal('0.25'), 'value': Decimal('12.3410')}
  - `yearyields.dates`: строк 1, поля from, till
  - первая строка: {'from': '2014-01-06', 'till': '2026-09-22'}
- история кривой (`history/engines/stock/zcyc.json`): блоки params
  - `params`: строк 13651, поля b1, b2, b3, g1, g2, g3, g4, g5, g6, g7, g8, g9, t1, tradedate, tradetime
  - первая строка: {'tradedate': '2026-09-22', 'tradetime': '09:10:00', 'b1': Decimal('1575.920309'), 'b2': Decimal('-441.120970'), 'b3': Decimal('-376.631921'), 't1': Decimal('0.517425'), 'g1': Decimal('0.147542'), 'g2': Decimal('-7.247082'), 'g3': Decimal('5.791530'), 'g4': Decimal('6.285446'), 'g5': Decimal('4.341030'), 'g6': Decimal('0.309931'), 'g7': Decimal('-3.639310'), 'g8': Decimal('0.000000'), 'g9': Decimal('0.000000')}

## 3. Облигационные индексы

Инструментов рынка индексов — 868, облигационных индексов среди них — **153** (отбор по коду: RGB — государственные, RUCB — корпоративные, RUMB — муниципальные).

| Индекс | Наименование |
|---|---|
| `RGBI` | Индекс Мосбиржи гос обл RGBI |
| `RGBILP` | RGBILP |
| `RGBITR` | Индекс Мосбиржи гос обл RGBITR |
| `RUCBCP2A3A` | RUCBCP2A3A |
| `RUCBCP2A3A3Y` | RUCBCP2A3A3Y |
| `RUCBCP2A3A5Y` | RUCBCP2A3A5Y |
| `RUCBCP2B3B` | RUCBCP2B3B |
| `RUCBCP3A3YNS` | RUCBCP3A3YNS |
| `RUCBCP3A5YNS` | RUCBCP3A5YNS |
| `RUCBCP3Y` | Индекс Мосбиржи корп обл CBICP 3-5 |
| `RUCBCP3YNS` | RUCBCP3YNS |
| `RUCBCP5Y` | Индекс Мосбиржи корп обл CBICP 1-3 |
| `RUCBCP5YNS` | RUCBCP5YNS |
| `RUCBCPA2A` | RUCBCPA2A |
| `RUCBCPA2A3Y` | RCBCPA2A3Y |
| `RUCBCPA2A5Y` | RUCBCPA2A5Y |
| `RUCBCPA3YNS` | RUCBCPA3YNS |
| `RUCBCPA5YNS` | RUCBCPA5YNS |
| `RUCBCPAA3YNS` | RUCBCPAA3YNS |
| `RUCBCPAA5YNS` | RUCBCPAA5YNS |
| `RUCBCPAAANS` | RUCBCPAAANS |
| `RUCBCPAANS` | RUCBCPAANS |
| `RUCBCPANS` | RUCBCPANS |
| `RUCBCPB2B` | RUCBCPB2B |
| `RUCBCPB2B3B` | RUCBCPB2B3B |
| `RUCBCPBBBNS` | RUCBCPBBBNS |
| `RUCBCPNS` | RUCBCPNS |
| `RUCBHYCP` | Индекс МосБиржи ВДО ПИР CP |
| `RUCBHYTR` | Индекс МосБиржи ВДО ПИР TR |
| `RUCBICP` | Индекс Мосбиржи корп обл CBICP |
| `RUCBICP1Y` | RUCBICP1Y |
| `RUCBICP3+` | CBI CP 3+ |
| `RUCBICPB` | CBI CP B |
| `RUCBICPB3Y` | CBI CP B 3Y |
| `RUCBICPBB` | CBI CP BB |
| `RUCBICPBB3+` | CBI CP BB 3+ |
| `RUCBICPBB3Y` | CBI CP BBB 3Y |
| `RUCBICPBB5Y` | CBI CP BB 5Y |
| `RUCBICPBBB` | CBI CP BBB |
| `RUCBICPBBB3+` | CBI CP BBB 3+ |
| `RUCBICPBBB3Y` | CBI CP BBB 3Y |
| `RUCBICPBBB5Y` | CBI CP BBB 5Y |
| `RUCBICPL1` | RUCBICPL1 |
| `RUCBICPL2` | RUCBICPL2 |
| `RUCBICPL3` | RUCBICPL3 |
| `RUCBITR` | Индекс Мосбиржи корп обл CBITR |
| `RUCBITR1Y` | RUCBITR1Y |
| `RUCBITR3+` | CBI TR 3+ |
| `RUCBITRB` | CBI TR B |
| `RUCBITRB3Y` | CBI TR B 3Y |
| `RUCBITRBB` | CBI TR BB |
| `RUCBITRBB3+` | CBI TR BB 3+ |
| `RUCBITRBB3Y` | CBI TR BB 3Y |
| `RUCBITRBB5Y` | CBI TR BB 5Y |
| `RUCBITRBBB` | CBI TR BBB |
| `RUCBITRBBB3+` | CBI TR BBB 3+ |
| `RUCBITRBBB3Y` | CBI TR BBB 3Y |
| `RUCBITRBBB5Y` | CBI TR BBB 5Y |
| `RUCBITRL1` | RUCBITRL1 |
| `RUCBITRL2` | RUCBITRL2 |
| `RUCBITRL3` | RUCBITRL3 |
| `RUCBKEYCP` | RUCBKEYCP |
| `RUCBKEYTR` | RUCBKEYTR |
| `RUCBRNCP` | RUCBRNCP |
| `RUCBRNTR` | RUCBRNTR |
| `RUCBTR2A3A` | RUCBTR2A3A |
| `RUCBTR2A3A3Y` | RUCBTR2A3A3Y |
| `RUCBTR2A3A5Y` | RUCBTR2A3A5Y |
| `RUCBTR2B3B` | RUCBTR2B3B |
| `RUCBTR3A3YNS` | RUCBTR3A3YNS |
| `RUCBTR3A5YNS` | RUCBTR3A5YNS |
| `RUCBTR3Y` | Индекс Мосбиржи корп обл CBITR 3-5 |
| `RUCBTR3YNS` | RUCBTR3YNS |
| `RUCBTR5Y` | Индекс Мосбиржи корп обл CBITR 1-3 |
| `RUCBTR5YNS` | RUCBTR5YNS |
| `RUCBTRA2A` | RUCBTRA2A |
| `RUCBTRA2A3Y` | RCBTRA2A3Y |
| `RUCBTRA2A5Y` | RUCBTRA2A5Y |
| `RUCBTRA3YNS` | RUCBTRA3YNS |
| `RUCBTRA5YNS` | RUCBTRA5YNS |
| `RUCBTRAA3YNS` | RUCBTRAA3YNS |
| `RUCBTRAA5YNS` | RUCBTRAA5YNS |
| `RUCBTRAAANS` | RUCBTRAAANS |
| `RUCBTRAANS` | RUCBTRAANS |
| `RUCBTRANS` | RUCBTRANS |
| `RUCBTRB2B` | RUCBTRB2B |
| `RUCBTRB2B3B` | RUCBTRB2B3B |
| `RUCBTRBBBNS` | RUCBTRBBBNS |
| `RUCBTRNS` | RUCBTRNS |
| `RUGBICP10Y` | RUGBICP10Y |
| `RUGBICP1Y` | RUGBICP1Y |
| `RUGBICP3Y` | RUGBICP3Y |
| `RUGBICP5+` | RUGBICP5+ |
| `RUGBICP5Y` | RUGBICP5Y |
| `RUGBICP5Y7Y` | RUGBICP5Y7Y |
| `RUGBICP7Y+` | RUGBICP7Y+ |
| `RUGBINFCP` | RUGBINFCP |
| `RUGBINFTR` | RUGBINFTR |
| `RUGBITR10Y` | RUGBITR10Y |
| `RUGBITR1Y` | RUGBITR1Y |
| `RUGBITR3Y` | RUGBITR3Y |
| `RUGBITR5+` | RUGBITR5+ |
| `RUGBITR5Y` | RUGBITR5Y |
| `RUGBITR5Y7Y` | RUGBITR5Y7Y |
| `RUGBITR7Y+` | RUGBITR7Y+ |
| `RUMBCP3+NS` | RUMBCP3+NS |
| `RUMBCP3A3+NS` | RUMBCP3A3+NS |
| `RUMBCP3A3YNS` | RUMBCP3A3YNS |
| `RUMBCP3YNS` | RUMBCP3YNS |
| `RUMBCPA3+NS` | RUMBCPA3+NS |
| `RUMBCPA3YNS` | RUMBCPA3YNS |
| `RUMBCPAA3+NS` | RUMBCPAA3+NS |
| `RUMBCPAA3YNS` | RUMBCPAA3YNS |
| `RUMBCPAAANS` | RUMBCPAAANS |
| `RUMBCPAANS` | RUMBCPAANS |
| `RUMBCPANS` | RUMBCPANS |
| `RUMBCPBBBNS` | RUMBCPBBBNS |
| `RUMBCPNS` | RUMBCPNS |
| `RUMBICP` | Индекс Мосбиржи мун обл MOEX MBICP |
| `RUMBICP1Y` | RUMBICP1Y |
| `RUMBICP3+` | MBI CP 3Y+ |
| `RUMBICP3Y` | MBI CP 3Y |
| `RUMBICPBB` | MBI CP BB |
| `RUMBICPBB3Y` | MBI CP BB 3Y |
| `RUMBICPBBB` | MBI CP BBB |
| `RUMBICPBBB3+` | MBI CP BBB 3+ |
| `RUMBICPBBB3Y` | MBI TR BBB 3Y |
| `RUMBICPL1` | RUMBICPL1 |
| `RUMBICPL3` | RUMBICPL3 |
| `RUMBITR` | Индекс Мосбиржи мун обл MBITR |
| `RUMBITR1Y` | RUMBITR1Y |
| `RUMBITR3+` | MBI TR 3Y+ |
| `RUMBITR3Y` | MBI TR 3Y |
| `RUMBITRBB` | MBI TR BB |
| `RUMBITRBB3Y` | MBI TR BB 3Y |
| `RUMBITRBBB` | MBI TR BBB |
| `RUMBITRBBB3+` | MBI TR BBB 3+ |
| `RUMBITRBBB3Y` | MBI TR BBB 3Y |
| `RUMBITRL1` | RUMBITRL1 |
| `RUMBITRL3` | RUMBITRL3 |
| `RUMBTR3+NS` | RUMBTR3+NS |
| `RUMBTR3A3+NS` | RUMBTR3A3+NS |
| `RUMBTR3A3YNS` | RUMBTR3A3YNS |
| `RUMBTR3YNS` | RUMBTR3YNS |
| `RUMBTRA3+NS` | RUMBTRA3+NS |
| `RUMBTRA3YNS` | RUMBTRA3YNS |
| `RUMBTRAA3+NS` | RUMBTRAA3+NS |
| `RUMBTRAA3YNS` | RUMBTRAA3YNS |
| `RUMBTRAAANS` | RUMBTRAAANS |
| `RUMBTRAANS` | RUMBTRAANS |
| `RUMBTRANS` | RUMBTRANS |
| `RUMBTRBBBNS` | RUMBTRBBBNS |
| `RUMBTRNS` | RUMBTRNS |

`RGBITR`: дней 5934, первый 2002-12-30, поля BOARDID, CAPITALIZATION, CLOSE, CURRENCYID, DECIMALS, DIVISOR, DURATION, HIGH, LOW, NAME, OPEN, RECALC_DATE, SECID, SHORTNAME, TRADEDATE, TRADE_SESSION_DATE, TRADINGSESSION, VALUE, VOLUME, YIELD

`RUCBITR`: дней 5094, первый 2002-12-31, поля BOARDID, CAPITALIZATION, CLOSE, CURRENCYID, DECIMALS, DIVISOR, DURATION, HIGH, LOW, NAME, OPEN, RECALC_DATE, SECID, SHORTNAME, TRADEDATE, TRADE_SESSION_DATE, TRADINGSESSION, VALUE, VOLUME, YIELD

## 4. Уведомления биржи: приостановка, риск-сектор, делистинг

Карточка выпуска `RU000A1061K1` — поля описания:

- `AMORTBOND` = 1
- `BOND_SUBTYPE` = До погашения
- `BOND_TYPE` = Амортизируемая облигация
- `COUPONDATE` = 2026-10-15
- `COUPONFREQUENCY` = 12
- `COUPONPERCENT` = 13.6
- `COUPONVALUE` = 5.59
- `DAYSTOREDEMPTION` = 173
- `DECISIONDATE` = 2023-03-29
- `EMITENTMISMATCHCUR` = 0
- `EMITTER_ID` = 14218
- `EVENINGSESSION` = 1
- `FACEUNIT` = SUR
- `FACEVALUE` = 500
- `GROUP` = stock_bonds
- `GROUPNAME` = Облигации
- `HASDEFAULT` = 1
- `HASPROSPECTUS` = 1
- `HASTECHNICALDEFAULT` = 1
- `HIGHRISK` = 1
- `INITIALFACEVALUE` = 1000
- `ISCONCESSIONAGREEMENT` = 0
- `ISIN` = RU000A1061K1
- `ISQUALIFIEDINVESTORS` = 0
- `ISSUEDATE` = 2023-04-04
- `ISSUENAME` = Биржевые облигации процентные неконвертируемые бездокументарные с централизованным учетом прав серии БО-001Р-03
- `ISSUESIZE` = 5000000
- `LATNAME` = EvroTrans BO-001P-03
- `LISTLEVEL` = 3
- `MATDATE` = 2027-03-14
- `MORNINGSESSION` = 1
- `NAME` = ЕвроТранс БО-001Р-03
- `PROGRAMREGISTRYNUMBER` = 4-80110-H-001P-02E
- `REGISTRY_DATE` = 2023-03-29
- `REGNUMBER` = 4B02-03-80110-H-001P
- `SECID` = RU000A1061K1
- `SHORTNAME` = ЕвроТранс3
- `STARTDATEMOEX` = 2023-04-04
- `TYPE` = exchange_bond
- `TYPENAME` = Биржевая облигация
- `WEEKENDSESSION` = 1

Режимы торгов выпуска — 43:

| Режим | Торгуется | Первая дата | Последняя дата |
|---|---|---|---|
| TQCB (Т+: Облигации - безадрес.) | 0 | 2023-04-04 | 2026-08-05 |
| TQRD (Т+: Облигации Д - безадрес.) | 1 | 2026-08-06 | 2026-09-21 |
| LIQB (Продажа обеспечения бирж.рынок - безадрес.) | 1 | None | None |
| RPEY (РЕПО в ин.валюте (CNY) - адрес.) | 0 | None | None |
| RPEU (РЕПО в ин. валюте (USD) - адрес.) | 0 | None | None |
| RPEO (РЕПО в ин. валюте (EUR) - адрес.) | 0 | None | None |
| EQRP (РЕПО с ЦК 1 день - безадрес.) | 0 | 2024-09-04 | 2025-10-10 |
| EQRD (РЕПО с ЦК 1 день (USD) - безадрес.) | 0 | None | None |
| EQRE (РЕПО с ЦК 1 день (EUR) - безадрес.) | 0 | None | None |
| EQRY (РЕПО с ЦК 1 день (CNY) - безадрес.) | 0 | None | None |
| EQWP (РЕПО с ЦК 7 дн. - безадрес.) | 0 | 2024-12-28 | 2025-06-11 |
| EQWD (РЕПО с ЦК 7 дней (USD) - безадрес.) | 0 | None | None |
| EQWE (РЕПО с ЦК 7 дней (EUR) - безадрес.) | 0 | None | None |
| EQWY (РЕПО с ЦК 7 дней (CNY) - безадрес.) | 0 | None | None |
| LIQR (РЕПО с ЦК: Урегулирование - безадрес.) | 1 | None | None |
| PSRP (РЕПО с ЦК - адрес.) | 0 | 2023-07-26 | 2026-07-27 |
| PSRD (РЕПО с ЦК (USD) - адрес.) | 0 | None | None |
| PSRE (РЕПО с ЦК (EUR) - адрес.) | 0 | None | None |
| PSRY (РЕПО с ЦК (CNY) - адрес.) | 0 | 2023-05-18 | 2023-05-19 |
| PSRK (РЕПО с ЦК (KZT) - адрес.) | 0 | None | None |
| PSRB (РЕПО с ЦК (BYN) - адрес.) | 0 | None | None |
| PSOB (РПС: Облигации - адрес.) | 0 | 2025-09-24 | 2025-12-15 |
| PSDB (РПС: Облигации Д - адрес.) | 1 | 2026-09-09 | 2026-09-09 |
| PSAU (Размещение - адрес.) | 0 | 2023-04-04 | 2023-05-18 |
| PTOB (РПС с ЦК: Облигации - адрес.) | 0 | None | None |
| PTDB (РПС с ЦК: Д Облигации - адрес.) | 1 | None | None |
| OCBR (OTC: Облигации с ЦК - двусторонние) | 0 | None | None |
| OCBU (OTC: Облигации с ЦК (USD) - двусторонние) | 0 | None | None |
| OCBY (OTC: Облигации с ЦК (CNY) - двусторонние) | 0 | None | None |
| OCAR (OTC: Облигации с ЦК адрес. - двусторонние) | 0 | None | None |
| OCAY (OTC: Облигации с ЦК адрес. (CNY) - двусторонние) | 0 | None | None |
| OCAU (OTC: Облигации с ЦК адрес. (USD) - двусторонние) | 0 | None | None |
| RPMO (РЕПО-М - адрес.) | 0 | 2026-04-14 | 2026-06-29 |
| RPEK (РЕПО-М в ин. валюте: (KZT) - адрес.) | 0 | None | None |
| RPEB (РЕПО-М в ин. валюте: (BYN) - адрес.) | 0 | None | None |
| CIQR (Урегулирование РЕПО с ЦК Нерезиденты - безадрес.) | 1 | None | None |
| CIQB (Урегулирование с ЦК Нерезиденты - безадрес.) | 1 | None | None |
| CTOB (РПС с ЦК Нерезиденты: Облигации - адрес.) | 1 | None | None |
| CPMO (РЕПО-M Нерезиденты - адрес.) | 0 | None | None |
| CPEU (РЕПО-M Нерезиденты (USD) - адрес.) | 0 | None | None |
| CPEO (РЕПО-M Нерезиденты (EUR) - адрес.) | 0 | None | None |
| CPEY (РЕПО-M Нерезиденты (CNY) - адрес.) | 0 | None | None |
| QBND (Квоты) | 0 | None | None |

## 5. Связь с Cbonds через ISIN

Эмитентов в списке наблюдения — 329, ISIN у их выпусков — **2272** (у всего справочника агрегатора — 2876); торгуемых облигаций в ISS — **3163**; совпало по ISIN — **816**.

Совпадение считается по торгуемым: выпуск, погашенный десять лет назад, в перечне ISS отсутствует правомерно, и разность здесь не пробел связи. **Связь прямая** — SECID торгуемой облигации равен её ISIN, и переходника не требуется.

| Режим торгов | Выпусков списка |
|---|---|
| TQCB — Т+ Облигации, обычный | 724 |
| TQOD — Т+ Облигации (расчёты в валюте) | 52 |
| TQOY — Т+ Облигации (юани) | 24 |
| TQRD — Т+ Облигации Д — **сектор повышенного риска** | 16 |

В секторе повышенного риска — **16** выпусков списка. Это готовый признак: перевод в него биржа датирует, и у ЕвроТранса он приходится на 06.08.2026 — за две недели до дефолта Кириллицы и задолго до нашей отчётной даты.

- `RU000A1061K1` — ЕвроТранс, БО-001P-03 (5029169023)
- `RU000A1082G5` — ЕвроТранс, 002Р-01 (5029169023)
- `RU000A108D81` — ЕвроТранс, 002Р-02 (5029169023)
- `RU000A108FS8` — Антерра, БО-02 (7730176955)
- `RU000A109S83` — Монополия, 001Р-01 (7810766685)
- `RU000A10A133` — ЕвроТранс, БО-001Р-04 (5029169023)
- `RU000A10A141` — ЕвроТранс, БО-001Р-05 (5029169023)
- `RU000A10A3Q3` — Антерра, БО-03 (7730176955)
- `RU000A10ATS0` — ЕвроТранс, БО-001Р-06 (5029169023)
- `RU000A10BB75` — ЕвроТранс, БО-001Р-07 (5029169023)
- `RU000A10BVW6` — ЕвроТранс, 01 (5029169023)
- `RU000A10BWL7` — Монополия, 001Р-05 (7810766685)
- `RU000A10C5Z7` — Монополия, 001Р-06 (7810766685)
- `RU000A10CFH8` — Монополия, 001Р-07 (7810766685)
- `RU000A10CZB9` — ЕвроТранс, БО-001Р-08 (5029169023)
- `RU000A10E4X3` — ЕвроТранс, БО-001Р-09 (5029169023)

---

Запросов к источнику 0, ответов с диска 116.
