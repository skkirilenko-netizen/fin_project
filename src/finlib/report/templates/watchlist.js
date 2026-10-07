/* Согласованный интерфейс: все оценки и величины поступают готовыми. */
'use strict';
const data = JSON.parse(document.getElementById('dataset').textContent);
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
  '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
}[ch]));
const pageSize = 40;
const state = {basket:'', group:'', bonds:data.rows.some(r => r.bonds === '1') ? '1' : '',
  search:'', page:0, event:'all', allEvents:false};
const baskets = data.baskets;
const labels = {status:'Переход в «Дефолт»', first:'Первое появление',
  grace:'Окончание льготы', rating:'Рейтинговое действие'};
const views = {home:'Обзор дня', issuers:'Эмитенты', summary:'Сводка', sources:'Источники'};
let returnFocus = null;

function go(view) {
  if (!views[view]) return;
  closeDrawer();
  Object.keys(views).forEach(key => $(key).hidden = key !== view);
  document.querySelectorAll('.nav-item').forEach(button => {
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $('crumb').textContent = views[view];
  window.scrollTo(0, 0);
}
function badge(row) {
  return `<span class="badge ${esc(row.basket)}">${esc(row.basketName)}</span>`;
}
function issuerName(event) {
  const known = event.inn && data.rows.some(row => row.inn === event.inn);
  const name = known ? `<button class="issuerbutton" data-inn="${esc(event.inn)}">${esc(event.name)}</button>` : `<strong>${esc(event.name)}</strong>`;
  return name + `<div class="subtle">${event.inn ? 'ИНН ' + esc(event.inn) : 'Привязка к эмитенту не установлена'}</div>`;
}
// Строка — запись источника: переходы одной записи названы вместе, по датам.
const kindsOf = event => event.kinds || [event.kind];
function eventHTML(event) {
  const title = [...new Set(kindsOf(event).map(kind => labels[kind] || 'Событие'))].join(' · ');
  return `<div class="event"><div>${issuerName(event)}</div><details><summary><div class="event-title">${esc(title)}</div><span class="subtle">${esc(event.eventOn)} · раскрыть сведения источника</span></summary><p>${esc(event.text)}</p><div class="event-dates subtle"><span>Исходная дата: ${esc(event.eventOn)}</span><span>Доставка: ${esc(event.deliveredAt)}</span><span>Первый вывод: ${esc(event.firstPrintedOn)}</span></div></details><span class="chevron">›</span></div>`;
}
function eventRender() {
  const filtered = data.events.filter(event => state.event === 'all' || kindsOf(event).includes(state.event));
  const shown = state.allEvents ? filtered : filtered.slice(0, 7);
  $('event-tabs').innerHTML = [['all', 'Все'], ...Object.entries(labels)].map(([key, label]) => {
    const count = key === 'all' ? data.events.length : data.events.filter(event => kindsOf(event).includes(key)).length;
    return `<button class="chip ${state.event === key ? 'active' : ''}" data-event="${key}" aria-pressed="${state.event === key}">${esc(label)} · ${count}</button>`;
  }).join('');
  $('events').innerHTML = shown.map(eventHTML).join('') || '<p class="muted" style="padding:20px">' + (data.urgentAvailable ? 'Нет выявленных уведомлений по доступным сведениям отчёта.' : 'Срочные уведомления не установлены: сохранённый раздел отсутствует.') + '</p>';
  $('event-count').textContent = data.urgentAvailable ? `Показано ${shown.length} из ${filtered.length} уведомлений` : 'Перечень уведомлений неизвестен';
  $('more-events').hidden = filtered.length <= 7;
  $('more-events').textContent = state.allEvents ? 'Свернуть' : 'Показать все';
  document.querySelectorAll('[data-event]').forEach(button => button.onclick = () => {
    state.event = button.dataset.event; state.allEvents = false; eventRender();
  });
  bindIssuerButtons($('events'));
}
function groupOptions() {
  const available = [...new Set(data.rows.filter(row => !state.basket || row.basket === state.basket).flatMap(row => row.subgroups).filter(Boolean))];
  if (!available.includes(state.group)) state.group = '';
  $('group').innerHTML = '<option value="">Все подгруппы</option>' + available.map(group => `<option>${esc(group)}</option>`).join('');
  $('group').value = state.group;
}
function filteredRows() {
  const query = state.search.trim().toLocaleLowerCase('ru');
  return data.rows.filter(row => (!state.basket || row.basket === state.basket) &&
    (!state.group || row.subgroups.includes(state.group)) &&
    (!state.bonds || row.bonds === state.bonds) &&
    (!query || (row.name + ' ' + row.inn).toLocaleLowerCase('ru').includes(query)));
}
function rowRender() {
  const filtered = filteredRows();
  state.page = Math.max(0, Math.min(state.page, Math.ceil(filtered.length / pageSize) - 1));
  const part = filtered.slice(state.page * pageSize, (state.page + 1) * pageSize);
  $('rows').innerHTML = part.map(row => `<tr><td><button class="issuerbutton" data-inn="${esc(row.inn)}">${esc(row.name)}</button><div class="inn">${esc(row.inn)}${row.bonds === '0' ? ' · без выпусков' : row.bonds === '?' ? ' · статус выпусков неизвестен' : ''}</div></td><td>${badge(row)}<div class="rowgroup">${esc(row.group)}</div></td><td><div class="clamp mainreason">${esc(row.reason) || 'Основание не указано'}</div></td><td><div class="clamp actiontext">${esc(row.action) || 'Действие не указано'}</div></td></tr>`).join('');
  $('empty').hidden = filtered.length > 0;
  $('shown').textContent = `Показано ${filtered.length} из ${data.rows.length}`;
  $('page-range').textContent = filtered.length ? `${state.page * pageSize + 1}–${Math.min((state.page + 1) * pageSize, filtered.length)} из ${filtered.length}` : '0 результатов';
  $('page-number').textContent = filtered.length ? `${state.page + 1} / ${Math.ceil(filtered.length / pageSize)}` : '0 / 0';
  $('prev').disabled = state.page === 0;
  $('next').disabled = (state.page + 1) * pageSize >= filtered.length;
  const tags = [state.bonds === '1' ? 'С выпусками в обращении' : state.bonds === '0' ? 'Без выпусков в обращении' : 'Все эмитенты'];
  if (state.basket) tags.push(baskets.find(([code]) => code === state.basket)[1]);
  if (state.group) tags.push(state.group);
  if (state.search.trim()) tags.push(`Поиск: ${state.search.trim()}`);
  $('active-filter').textContent = tags.join(' · ');
  $('filter-indicator').textContent = String([state.basket, state.group, state.bonds, state.search.trim()].filter(Boolean).length);
  $('basket').value = state.basket; $('bonds').value = state.bonds;
  $('basket-tabs').innerHTML = [['', 'Все'], ...baskets].map(([code, name]) => {
    const count = data.rows.filter(row => (!code || row.basket === code) && (!state.bonds || row.bonds === state.bonds)).length;
    return `<button data-queue="${esc(code)}" class="${state.basket === code ? 'active' : ''}" aria-pressed="${state.basket === code}">${esc(name)}<span>${count}</span></button>`;
  }).join('');
  document.querySelectorAll('[data-queue]').forEach(button => button.onclick = () => {
    state.basket = button.dataset.queue; state.page = 0; groupOptions(); rowRender();
  });
  bindIssuerButtons($('rows'));
}
function bindIssuerButtons(root) {
  root.querySelectorAll('[data-inn]').forEach(button => button.onclick = () => openIssuer(button.dataset.inn, button));
}
function openIssuer(inn, origin) {
  const row = data.rows.find(item => item.inn === inn);
  if (!row) return;
  returnFocus = origin;
  $('drawer-title').textContent = row.name;
  $('drawer-inn').textContent = `ИНН ${row.inn}${row.assessed ? ' · класс ' + row.assessed : ''}`;
  $('drawer-tags').innerHTML = badge(row) + row.subgroups.filter(Boolean).map(group => `<span class="badge">${esc(group)}</span>`).join('');
  const grounds = row.grounds.map(ground => `<h4>${esc(ground.name)}</h4>${ground.details.map(detail => `<p class="gn">${esc(detail)}</p>`).join('')}`).join('');
  const notes = row.notes.filter(Boolean).map(note => `<p class="gd">${esc(note)}</p>`).join('');
  const values = row.values.map(([name, shown]) => `<div class="v"><span class="vn">${esc(name)}</span><span class="vv">${esc(shown)}</span></div>`).join('');
  const card = row.cardLink ? `<a class="button" target="_blank" href="${esc(row.cardLink)}">Полная карточка эмитента ↗</a>` : '<p class="muted">Карточка эмитента не собрана.</p>';
  const csv = row.csvFields ? '<details class="more"><summary>Все сохранённые графы CSV</summary>' + Object.entries(row.csvFields).map(([name, value]) => `<p class="csv-field"><b>${esc(name)}</b>: ${esc(value) || 'не раскрыто'}</p>`).join('') + '</details>' : '';
  $('drawer-body').innerHTML = `<h3>Предписанные действия</h3><div class="actionblock">${row.actions.filter(Boolean).map(esc).join('<br>') || 'Не указаны'}</div><h3>Все основания</h3>${grounds}<p class="meta">${esc(row.coverage)}</p>${notes}<h3>Величины маршрута</h3>${values || '<p class="muted">В исходных данных не приведены</p>'}${row.chart || ''}<h3>Источник и отчётность</h3><div class="meta">${esc(row.origin)}</div><div class="meta">${row.sources.map(esc).join(' · ')}</div><div class="meta ${row.stale || row.overdue ? 'stale' : ''}">${esc(row.report_date)}${row.months !== null ? ' · ' + esc(row.months) + ' мес.' : ''}</div><div class="meta">Единица комплекта: ${esc(row.unit) || 'не установлена'}</div>${csv}${card}`;
  $('drawer').hidden = false; $('backdrop').hidden = false;
  document.body.style.overflow = 'hidden'; $('close').focus();
}
function closeDrawer() {
  const wasOpen = !$('drawer').hidden;
  $('drawer').hidden = true; $('backdrop').hidden = true; document.body.style.overflow = '';
  if (wasOpen) (returnFocus?.isConnected ? returnFocus : $('search')).focus();
}
function reset() {
  Object.assign(state, {basket:'', group:'', bonds:'', search:'', page:0});
  $('search').value = ''; groupOptions(); rowRender();
}
function fileLink(id, href) {
  if (href) $(id).setAttribute('href', href);
  else { $(id).setAttribute('aria-disabled', 'true'); $(id).textContent += ' — файл не сохранён'; }
}
document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => go(button.dataset.view));
document.querySelectorAll('[data-go]').forEach(button => button.onclick = () => go(button.dataset.go));
$('theme').onclick = () => {
  const dark = document.documentElement.dataset.theme !== 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('theme').textContent = dark ? 'Светлая тема' : 'Тёмная тема';
};
$('close').onclick = closeDrawer; $('backdrop').onclick = closeDrawer;
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') closeDrawer();
  if (event.key === 'Tab' && !$('drawer').hidden) {
    const focusable = [...$('drawer').querySelectorAll('button,a[href],summary')];
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last.focus();}
    else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first.focus();}
  }
});
$('reset').onclick = reset; $('empty-reset').onclick = reset;
$('filter-toggle').onclick = () => {
  $('filters').hidden = !$('filters').hidden;
  $('filter-toggle').setAttribute('aria-expanded', String(!$('filters').hidden));
};
$('search').oninput = event => {state.search = event.target.value; state.page = 0; rowRender();};
for (const key of ['basket','group','bonds']) $(key).onchange = event => {
  state[key] = event.target.value; state.page = 0;
  if (key === 'basket') groupOptions();
  rowRender();
};
$('prev').onclick = () => {state.page--; rowRender();};
$('next').onclick = () => {state.page++; rowRender();};
$('more-events').onclick = () => {state.allEvents = !state.allEvents; eventRender();};
$('basket').innerHTML = '<option value="">Все корзины</option>' + baskets.map(([code,name]) => `<option value="${esc(code)}">${esc(name)}</option>`).join('');
for (const id of ['day', 'side-date']) $(id).textContent = data.day;
$('period').textContent = data.period; $('report-name').textContent = data.reportName;
$('daily-period').textContent = data.period;
$('daily-changes').textContent = data.dailyChanges ?? 'не установлены';
$('total').textContent = data.rows.length;
$('urgent-total').textContent = data.urgentAvailable ? data.events.length : 'не установлены';
$('nav-count').textContent = data.events.length + data.late.length;
$('warnings').innerHTML = data.warnings.map(text => `<div class="notice"><span>◈</span><div>${esc(text).replace(/\n/g, '<br>')}</div></div>`).join('');
$('late-state').textContent = data.lateAvailable ? `записей: ${data.late.length} · исходные даты переходов, доставка и первый вывод` : 'В сохранённом отчёте не установлен: это не ноль событий';
$('late-events').innerHTML = data.late.map(eventHTML).join('') || '<p class="muted" style="padding:20px">' + (data.lateAvailable ? 'Новых пропущенных ключей по доступным сведениям не выявлено.' : 'Сведения о первом выводе в этом отчёте отсутствуют.') + '</p>';
bindIssuerButtons($('late-events'));
if (data.lateReportLink) {$('late-report-link').hidden = false; fileLink('late-report-link', data.lateReportLink);}
const sections = data.sections.filter(section => !section.title.startsWith('Доставлено с опозданием')).map(section => {
  let lines = section.lines;
  if (section.title.startsWith('Срочное')) {
    const start = lines.findIndex(line => line.includes('Уточнения сведений источника'));
    if (start < 0) return '';
    lines = lines.slice(start);
  }
  return `<details class="panel summary-notes"><summary>${esc(section.title.startsWith('Срочное') ? 'Уточнения сведений источника' : section.title)}</summary><div class="report-text">${esc(lines.join('\n'))}</div></details>`;
});
$('report-sections').innerHTML = sections.join('');
$('stats').innerHTML = data.stats.map(item => `<div class="panel summary-item"><b>${esc(item.value)}</b><span>${esc(item.label)}</span></div>`).join('');
$('limitations').innerHTML = '<div>' + esc(data.coverage) + '</div>' + data.limitations.map(text => `<div>${esc(text)}</div>`).join('');
$('sources-list').innerHTML = data.sources.map(item => `<div class="source-row"><div><strong>${esc(item.name)}</strong><p>${esc(item.note)}</p></div><span class="badge ${item.status === 'done' ? 'clear' : 'attention'}">${esc(item.status)}</span></div>`).join('');
fileLink('report-link', data.reportLink); fileLink('csv-link', data.csvLink);
eventRender(); groupOptions(); rowRender();
