"""Persistent storefront management, available only through the admin router."""
from aiogram import F
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
import bot as b

class Edit(StatesGroup):
    value = State()

async def catalog(page=0):
    cur = await b.db.execute('SELECT * FROM positions ORDER BY sort_order,quantity')
    items = await cur.fetchall()
    page = max(0, min(page, max(0, (len(items)-1)//8)))
    rows = [[b.btn(f"{'В продаже' if p['in_storefront'] else 'Скрыт'} · {p['title'] or str(p['quantity'])+' тапов'}", f"sf:item:{p['quantity']}:{page}")] for p in items[page*8:page*8+8]]
    nav = []
    if page: nav.append(b.btn('Назад', f'sf:list:{page-1}'))
    if (page+1)*8 < len(items): nav.append(b.btn('Далее', f'sf:list:{page+1}'))
    if nav: rows.append(nav)
    rows += [[b.btn('Добавить пакет', 'sf:add')], [b.btn('Админ-панель', 'ap:home')]]
    return f'<b>Товары витрины</b>\nВсего позиций: {len(items)}. Страница {page+1}.\n\nИзменения появятся на сайте после обновления страницы. Цены общие с ботом. Скрытие с витрины не меняет меню бота.', b.markup(*rows)

async def card(q, page=0):
    cur = await b.db.execute('SELECT * FROM positions WHERE quantity=?', (q,))
    p = await cur.fetchone()
    if not p: return await catalog(page)
    return (f"<b>{b.e(p['title'] or str(q)+' тапов')}</b>\nКоличество: {q}\nЦена: {p['stars']} звёзд / {p['rub']} ₽\nОписание: {b.e(p['description'] or 'не задано')}\nПорядок: {p['sort_order']} (меньше — выше)\nНа витрине: {'да' if p['in_storefront'] else 'нет'}", b.markup(
        [b.btn('Название', f'sf:edit:{q}:title'), b.btn('Описание', f'sf:edit:{q}:description')],
        [b.btn('Цены', f'sf:edit:{q}:prices'), b.btn('Порядок', f'sf:edit:{q}:sort_order')],
        [b.btn('Скрыть с витрины' if p['in_storefront'] else 'Показать на витрине', f'sf:toggle:{q}')],
        [b.btn('Удалить пакет', f'sf:delete:{q}')],
        [b.btn('К каталогу', f'sf:list:{page}')]))

@b.admin_router.callback_query(F.data.startswith('sf:'))
async def manage(cb, state):
    parts = cb.data.split(':'); action = parts[1]
    await state.clear()
    if action == 'list': view = await catalog(int(parts[2]))
    elif action == 'item': view = await card(int(parts[2]), int(parts[3]))
    elif action == 'toggle':
        q = int(parts[2])
        await b.db.execute('UPDATE positions SET in_storefront=1-in_storefront WHERE quantity=?', (q,)); await b.db.commit()
        view = await card(q)
    elif action in ('add', 'edit'):
        field = 'new' if action == 'add' else parts[3]
        prompts = {'new':'Введите количество тапов, цену в звёздах и рублях через пробел. Например: 300 110 145. Новый пакет появится на витрине и в меню бота.', 'title':'Введите название до 60 символов. Отправьте - для стандартного названия.', 'description':'Введите описание до 300 символов. Отправьте - для очистки.', 'prices':'Введите две положительные целые цены: звёзды рубли.', 'sort_order':'Введите порядок от 0 до 1000000. Меньшее число показывается выше.'}
        if field not in prompts: await cb.answer(); return
        await state.set_state(Edit.value)
        await state.update_data(field=field, qty=int(parts[2]) if action=='edit' else None)
        view = prompts[field], b.markup([b.btn('Отмена', 'sf:list:0')])
    elif action == 'delete':
        q = int(parts[2])
        view = 'Удалить пакет из витрины и меню бота? Существующие заказы сохранятся.', b.markup([b.btn('Да, удалить', f'sf:remove:{q}')], [b.btn('Отмена', f'sf:item:{q}:0')])
    elif action == 'remove':
        await b.db.execute('DELETE FROM positions WHERE quantity=?', (int(parts[2]),)); await b.db.commit()
        view = await catalog()
    else: await cb.answer(); return
    try: await cb.message.edit_text(view[0], reply_markup=view[1])
    except TelegramBadRequest as exc:
        if 'not modified' not in str(exc): raise
    await cb.answer()

@b.admin_router.message(Edit.value, F.text)
async def save(message, state):
    data = await state.get_data(); field = data['field']; q = data.get('qty'); text = message.text.strip()
    if text.startswith('/'):
        await state.clear(); await message.answer('Редактирование отменено. Откройте /admin.'); return
    if field in ('new', 'prices', 'sort_order'):
        values = text.split(); expected = {'new':3, 'prices':2, 'sort_order':1}[field]
        if len(values)!=expected or any(not v.isascii() or not v.isdigit() or len(v)>9 for v in values):
            await message.answer('Введите целые числа через пробел в указанном формате.'); return
        values = list(map(int, values))
        if field == 'sort_order': valid = 0 <= values[0] <= 1000000
        else: valid = all(1 <= v <= 999999999 for v in values) and (field!='new' or values[0]<=1000000)
        if not valid: await message.answer('Недопустимые значения. Количество: 1–1000000, цены должны быть положительными.'); return
        if field == 'new':
            q, stars, rub = values
            if await b.get_position(q): await message.answer('Такой пакет уже есть. Измените его в каталоге или введите другое количество.'); return
            await b.db.execute('INSERT INTO positions (quantity,stars,rub,sort_order,in_storefront) VALUES (?,?,?,?,1)', (q,stars,rub,q))
        elif field == 'prices': await b.db.execute('UPDATE positions SET stars=?,rub=? WHERE quantity=?', (*values,q))
        else: await b.db.execute('UPDATE positions SET sort_order=? WHERE quantity=?', (values[0],q))
    elif field in ('title', 'description'):
        if len(text) > (60 if field=='title' else 300): await message.answer('Текст слишком длинный.'); return
        await b.db.execute(f'UPDATE positions SET {field}=? WHERE quantity=?', ('' if text=='-' else text,q))
    else: return
    await b.db.commit(); await state.clear()
    text, kb = await card(q)
    await message.answer('Сохранено.\n\n'+text, reply_markup=kb)
