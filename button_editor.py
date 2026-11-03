"""Persistent presentation overrides. Callback payloads never change."""
import json
import unicodedata
from aiogram import F
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton
import bot as b

CATALOG = {
    'qty:max': 'Максимальное количество',
    'qty:custom': '✍️ Ввести своё количество',
    'confirm:yes': '✅ Всё верно',
    'confirm:edit': '✏️ Изменить',
    'confirm:cancel': '❌ Отменить',
    'adm:accept': '✅ Принять',
    'adm:decline': '❌ Отклонить',
    'adm:approve:rub': '✅ Оплата в ₽',
    'adm:approve:stars': '✅ Оплата в ⭐',
    'adm:reject': '❌ Отклонить',
    'adm:close': '🔒 Закрыть опрос',
    'adm:notify': '📣 Сообщить о выполнении',
    'ap:st:today': '📊 Статистика',
    'ap:ord:all': '📋 Заказы',
    'ap:pos': '📦 Позиции и цены',
    'ap:txt': '✏️ Тексты бота',
    'ap:bc': '📣 Рассылка',
    'ap:dest': '🛠 Группы и темы',
}
COLORS = {'': 'Стандартный', 'primary': 'Синий', 'success': 'Зелёный', 'danger': 'Красный'}
_overrides = {}

class Edit(StatesGroup):
    label = State()

def key_for(data):
    parts = data.split(':')
    if len(parts) >= 3 and parts[0] == 'adm' and parts[2].isdigit():
        return ':'.join(parts[:2] + parts[3:])
    return data

def valid_label(text):
    return (isinstance(text, str) and 1 <= len(text.strip()) <= 64
            and any(not ch.isspace() and unicodedata.category(ch)[0] != 'C' for ch in text)
            and not any(unicodedata.category(ch) == 'Cc' for ch in text))

def render(text, data):
    value = _overrides.get(key_for(data), {})
    return InlineKeyboardButton(text=value.get('text', text), callback_data=data,
                                style=value.get('style') or None)

async def load():
    _overrides.clear()
    cur = await b.db.execute("SELECT key,value FROM settings WHERE key LIKE 'button:%'")
    for row in await cur.fetchall():
        try:
            value = json.loads(row['value'])
            if isinstance(value, dict) and valid_label(value.get('text')) and value.get('style', '') in COLORS:
                _overrides[row['key'][7:]] = value
        except (ValueError, TypeError):
            continue

async def entries():
    return list(CATALOG.items()) + [(f'qty:{q}', str(q)) for q, _, _ in await b.get_positions()]

async def resolve(index):
    items = await entries()
    if index.startswith('q'):
        return next((item for item in items if item[0] == 'qty:' + index[1:]), None)
    if not index.isdigit() or not 0 <= int(index) < len(CATALOG):
        return None
    return items[int(index)]

async def save(key, default, text=None, style=None):
    current = dict(_overrides.get(key, {'text': default, 'style': ''}))
    if text is not None:
        if not valid_label(text): raise ValueError('Invalid button label')
        current['text'] = text.strip()
    if style is not None:
        if style not in COLORS: raise ValueError('Invalid button style')
        current['style'] = style
    await b.set_setting('button:' + key, json.dumps(current, ensure_ascii=False))
    _overrides[key] = current

async def reset(key):
    await b.db.execute('DELETE FROM settings WHERE key=?', ('button:' + key,))
    await b.db.commit()
    _overrides.pop(key, None)

def raw(text, data):
    # Editor navigation cannot be renamed, hidden or disabled.
    return InlineKeyboardButton(text=text, callback_data=data)

async def listing(page=0):
    items = await entries()
    page = min(max(page, 0), max((len(items)-1)//8, 0))
    rows = [[raw(label, f'be:open:{i if i < len(CATALOG) else "q" + items[i][0].split(":")[1]}')] for i, (_, label) in enumerate(items)
            if page*8 <= i < (page+1)*8]
    nav = []
    if page: nav.append(raw('Предыдущая', f'be:list:{page-1}'))
    if (page+1)*8 < len(items): nav.append(raw('Следующая', f'be:list:{page+1}'))
    if nav: rows.append(nav)
    rows += [[raw('Расположение и видимость меню', 'ap:menu')], [raw('В админ-панель', 'ap:home')]]
    return ('<b>Редактор кнопок</b>\n\nВыберите кнопку. Можно изменить подпись и цвет. '
            'Действие кнопки не меняется. Изменения видны в новых сообщениях и при обновлении меню.\n'
            f'Страница {page+1}.', b.markup(*rows))

async def detail(index, key, default):
    current = _overrides.get(key, {'text': default, 'style': ''})
    sample = render(default, key)
    sample.callback_data = 'be:sample'
    rows = [[sample], [raw('Изменить подпись', f'be:rename:{index}')]]
    rows += [[raw(('Выбран: ' if current.get('style', '') == color else '') + label,
                  f'be:color:{index}:{color or "default"}') for color, label in list(COLORS.items())[i:i+2]]
             for i in (0, 2)]
    rows += [[raw('Сбросить оформление', f'be:reset:{index}')], [raw('К списку', 'be:list:0')]]
    return ('<b>Оформление кнопки</b>\n\nИсходная подпись: ' + b.e(default) +
            '\nТекущая подпись: ' + b.e(current['text']) +
            '\n\nПервая кнопка — безопасный предпросмотр: заказ не создаётся. '
            'Цвет зависит от поддержки вашей версией Telegram.', b.markup(*rows))

@b.admin_router.callback_query(F.message.chat.type == 'private', F.data.startswith('be:'))
async def editor_callback(cb, state):
    from admin import show
    parts = cb.data.split(':')
    action = parts[1]
    if action == 'sample':
        await cb.answer('Предпросмотр: действие не выполняется'); return
    await state.clear()
    index = parts[2] if len(parts) > 2 else '0'
    if action == 'list':
        view = await listing(int(index) if index.isdigit() else 0)
    else:
        entry = await resolve(index)
        if not entry:
            await cb.answer('Список изменился. Откройте редактор заново.', show_alert=True); return
        key, default = entry
        if action == 'rename':
            await state.set_state(Edit.label)
            await state.update_data(button_key=key, button_default=default, button_index=index)
            view = ('Отправьте новую подпись: 1–64 символа, одной строкой. Можно использовать эмодзи.',
                    b.markup([raw('Отмена', f'be:open:{index}')]))
        else:
            if action == 'color':
                color = parts[3] if len(parts) > 3 else ''
                color = '' if color == 'default' else color
                if color not in COLORS:
                    await cb.answer('Неизвестный цвет', show_alert=True); return
                await save(key, default, style=color)
            elif action == 'reset': await reset(key)
            view = await detail(index, key, default)
    await show(cb, view)
    await cb.answer()

@b.admin_router.message(Edit.label)
async def editor_label(message, state):
    if not valid_label(message.text):
        await message.answer('Нужна непустая подпись до 64 символов, одной строкой. Или нажмите «Отмена».'); return
    data = await state.get_data()
    key, default = data['button_key'], data['button_default']
    if key not in dict(await entries()):
        await state.clear(); await message.answer('Позиция удалена. Откройте редактор заново.'); return
    await save(key, default, text=message.text)
    await state.clear()
    # Resolve again: positions may have been changed by another administrator.
    index = next(i for i, item in enumerate(await entries()) if item[0] == key)
    text, kb = await detail(str(index) if index < len(CATALOG) else 'q' + key.split(':')[1], key, default)
    await message.answer(text, reply_markup=kb)
