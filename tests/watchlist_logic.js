/* Синтетический DOM: проверка реального приложения, без UI-автоматизации. */
const fixture = /*PAYLOAD*/;
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {textContent:'', innerHTML:'', value:'',
    hidden:['drawer','backdrop','filters'].includes(id), disabled:false, isConnected:true,
    attrs:{}, setAttribute(key,value){this.attrs[key]=value;}, removeAttribute(key){delete this.attrs[key];},
    querySelectorAll(){return [];}, focus(){document.activeElement=this;}});
  return nodes.get(id);
}
node('dataset').textContent = JSON.stringify(fixture);
const document = {getElementById:node, querySelectorAll(){return [];}, addEventListener(){},
  documentElement:{dataset:{}}, body:{style:{}}};
const window = {scrollTo(){}};
function check(ok, label) {if (!ok) throw new Error(label);}
/*APPLICATION*/
check(node('total').textContent === 93, 'исходный счётчик');
check(node('urgent-total').textContent === 1, 'счётчик уведомлений из данных');
check(node('late-events').innerHTML.includes('14:29:06 МСК'), 'фактическая доставка');
check(node('late-events').innerHTML.includes('Первый вывод: 03.01.2090'), 'первый вывод');
check(node('late-events').innerHTML.includes('эмитент не установлен'), 'непривязанный ключ');
check((node('rows').innerHTML.match(/<tr>/g) || []).length === 40, 'первая страница');
node('next').onclick();
check(node('page-range').textContent === '41–80 из 93', 'вторая страница');
node('next').onclick();
check(node('page-range').textContent === '81–93 из 93' && node('next').disabled, 'последняя страница');
node('search').oninput({target:{value:'0000000001'}});
check(node('shown').textContent === 'Показано 1 из 93', 'поиск по ИНН');
check(node('page-number').textContent === '1 / 1', 'поиск сбрасывает страницу');
node('search').oninput({target:{value:'тЕсТ 1'}});
check(filteredRows().length === 11, 'поиск по названию без регистра');
node('basket').onchange({target:{value:'attention'}});
node('group').onchange({target:{value:'данные'}});
check(filteredRows().every(row => row.basket === 'attention' && row.subgroups.includes('данные')), 'совместные фильтры');
check(filteredRows().length === 3, 'точный состав совместного результата');
check(node('active-filter').textContent.includes('Поиск: тЕсТ 1'), 'активный поиск виден');
node('filter-toggle').onclick();
check(!node('filters').hidden && node('filter-toggle').attrs['aria-expanded'] === 'true', 'скрываемые фильтры');
node('search').oninput({target:{value:'неизвестный-эмитент'}});
check(!node('empty').hidden && node('shown').textContent === 'Показано 0 из 93', 'пустой результат');
node('empty-reset').onclick();
check(filteredRows().length === 93 && state.page === 0 && state.group === '', 'полный сброс');
node('basket').onchange({target:{value:'out_of_scope'}});
check(filteredRows().length === 23, 'четвёртая корзина доступна');
openIssuer('0000000001', node('search'));
check(!node('drawer').hidden && node('drawer-body').innerHTML.includes('123 млн руб.'), 'подробности и единицы');
check(node('drawer-body').innerHTML.includes('Второе основание'), 'все основания');
check(node('drawer-body').innerHTML.includes('проверять'), 'все действия');
check(node('drawer-body').innerHTML.includes('Карточка эмитента не собрана'), 'нет ложной ссылки');
node('close').onclick();
check(node('drawer').hidden && document.body.style.overflow === '', 'закрытие');
go('sources'); check(!node('sources').hidden && node('home').hidden, 'навигация');
node('theme').onclick(); check(document.documentElement.dataset.theme === 'dark', 'тёмная тема');
node('theme').onclick(); check(document.documentElement.dataset.theme === 'light', 'светлая тема');
print('интерфейс проверен');
