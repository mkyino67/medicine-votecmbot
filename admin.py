"""Administrator menu, pricing, editable texts, statistics and broadcasts."""
import asyncio
import logging
import re
from aiogram import Bot, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
import bot as b

def back(data='ap:home'): return [b.btn('⬅️ Назад',data)]
def price(text):
    nums=re.findall(r'\d+',text)
    if len(nums)!=2 or any(len(n)>9 for n in nums): return None
    s,r=map(int,nums)
    return (s,r) if s>0 and r>0 else None
async def show(cb,view):
    text,kb=view
    try: await cb.message.edit_text(text,reply_markup=kb)
    except TelegramBadRequest as exc:
        if 'not modified' not in str(exc): raise
async def home():
    s=await b.compute_stats('today'); opened=await b.orders_open()
    return (f"⚙️ <b>Админ-панель</b>\n\nСегодня: оплат {s['paid']} · {s['rub']} ₽ / {s['stars']}⭐\nЖдут принятия: {s['approval']} · проверки оплаты: {s['pending']}\nПриём заказов: {'🟢 включён' if opened else '⏸ приостановлен'}",b.markup([b.btn('📊 Статистика','ap:st:today'),b.btn('📋 Заказы','ap:ord:all')],[b.btn('📦 Позиции и цены','ap:pos'),b.btn('✏️ Тексты бота','ap:txt')],[b.btn('📣 Рассылка','ap:bc'),b.btn('🛠 Группы и темы','ap:dest')],[b.btn('Товары витрины','sf:list:0'),b.btn('Редактор кнопок','be:list:0')],[b.btn('⏸ Приостановить приём' if opened else '▶️ Возобновить приём','ap:tog')]))
async def stats(period):
    labels=dict(b.PERIODS)
    if period not in labels: period='today'
    s=await b.compute_stats(period)
    avg=round(s['rub']/s['rub_cnt']) if s['rub_cnt'] else 0
    top=', '.join(f'{q} ({c})' for q,c in s['top']) or '—'
    text=f"📊 <b>Статистика — {labels[period]}</b>\n\nСоздано: {s['created']}\nОплачено: {s['paid']} · отклонено: {s['rejected']}\nПродано: {s['sold']} тапов · выполнено: {s['tapped']}\n\n💰 <b>Выручка</b>\n{s['rub']} ₽ (заказов {s['rub_cnt']}, средний чек {avg} ₽)\n{s['stars']}⭐ (заказов {s['stars_cnt']})\nСпособ не указан: {s['unknown']}\n\n👥 <b>Пользователи</b>\nВсего: {s['users_total']} · заблокировали: {s['users_blocked']}\nНовых: {s['users_new']} · активных: {s['users_active']}\nПокупателей: {s['buyers']}\n\nПопулярные позиции: {top}\n\nЖдут принятия: {s['approval']}\nЖдут проверки оплаты: {s['pending']}\nВ работе: {s['in_progress']}\nЖдут оплаты: {s['awaiting']}"
    tabs=[b.btn(label,f'ap:st:{key}') for key,label in b.PERIODS]
    return text,b.markup(tabs[:2],tabs[2:],back())
async def orders(flt):
    where="status IN ('pending_approval','pending_review')" if flt=='pending' else '1=1'
    cur=await b.db.execute(f'SELECT id,quantity,price_rub,status FROM orders WHERE {where} ORDER BY id DESC LIMIT 10')
    rows=[[b.btn(f"#{o['id']} · {o['quantity']} · {o['price_rub']}₽ · {b.STATUS_LABELS.get(o['status'],o['status'])}",f"ap:o:{o['id']}")] for o in await cur.fetchall()]
    return '📋 <b>Последние заказы</b>\n\nНажмите на заказ.' if rows else 'Заказов пока нет.',b.markup(*rows,[b.btn('Все','ap:ord:all'),b.btn('⏳ Ждут решения','ap:ord:pending')],back())
async def card(oid):
    o=await b.get_order(oid)
    if not o: return await orders('all')
    buyer='@'+b.e(o['buyer_username']) if o['buyer_username'] else 'без юзернейма'
    return f"🧾 <b>Заказ #{o['id']}</b> — {b.STATUS_LABELS.get(o['status'],o['status'])}\n\n👤 Покупатель: {buyer} (id {o['buyer_id']})\n📦 Количество: {o['quantity']}\n🔗 Ссылка: {b.e(o['link'])}\n💬 Юзернейм: {b.e(o['target_username'])}\n💰 Цена: {o['price_stars']}⭐ / {o['price_rub']}₽\n💳 Способ оплаты: {b.e(o['pay_method'] or '—')}\n🎯 Оттапано: {o['tapped']} из {o['quantity']}\n🕓 Создан (UTC): {o['created_at']}\n✅ Одобрен (UTC): {o['approved_at'] or '—'}",b.markup(back('ap:ord:all'))
async def positions():
    cur=await b.db.execute('SELECT * FROM positions ORDER BY quantity')
    rows=[[b.btn(f"{'🔘 В меню' if p['in_menu'] else '▫️ Скрыта'}: {p['quantity']} — {p['stars']}⭐ / {p['rub']}₽",f"ap:p:{p['quantity']}")] for p in await cur.fetchall()]
    return '📦 <b>Позиции и цены</b>\n\nНажмите на позицию для редактирования.',b.markup(*rows,[b.btn('➕ Добавить позицию','ap:pa'),b.btn('🧩 Вид меню /start','ap:menu')],back())
async def position(qty):
    cur=await b.db.execute('SELECT * FROM positions WHERE quantity=?',(qty,)); p=await cur.fetchone()
    if not p: return await positions()
    return f"📦 <b>Позиция: {qty}</b>\n💰 Цена: {p['stars']}⭐ / {p['rub']}₽\nКнопка: {'показана' if p['in_menu'] else 'скрыта'}",b.markup([b.btn('🙈 Скрыть из меню' if p['in_menu'] else '👁 Показать в меню',f'ap:pv:{qty}')],[b.btn('✏️ Изменить цену',f'ap:pe:{qty}'),b.btn('🗑 Удалить',f'ap:pd:{qty}')],back('ap:pos'))
async def menu():
    cols,mx,custom=await b.menu_settings(); shown=[q for q,_,_ in await b.get_menu_positions()]
    hidden=[q for q,_,_ in await b.get_positions() if q not in shown]
    return f"🧩 <b>Вид меню /start</b>\n\nПоказаны: {', '.join(map(str,shown)) or 'нет'}\nСкрыты: {', '.join(map(str,hidden)) or 'нет'}\nКнопок в ряду: {cols}\nМаксимум: {'вкл' if mx else 'выкл'}\nСвой ввод: {'вкл' if custom else 'выкл'}",b.markup([b.btn(f'Кнопок в ряду: {cols}','ap:mcols')],[b.btn('Переключить максимум','ap:mmax')],[b.btn('Переключить свой ввод','ap:mcust')],[b.btn('👁 Показать все','ap:mall:1'),b.btn('🙈 Скрыть все','ap:mall:0')],[b.btn('👀 Показать как у клиента','ap:mprev')],back('ap:pos'))
async def texts():
    cur=await b.db.execute('SELECT key FROM texts'); changed={r['key'] for r in await cur.fetchall()}
    rows=[[b.btn(('✏️ ' if k in changed else '')+label,f'ap:t:{k}')] for k,(label,_,_) in b.DEFAULT_TEXTS.items()]
    return '✏️ <b>Тексты бота</b>',b.markup(*rows,back())
async def text_view(key):
    label,_,holders=b.DEFAULT_TEXTS[key]
    rows=[[b.btn('✏️ Изменить',f'ap:te:{key}')]]
    if await b.num('SELECT COUNT(*) FROM texts WHERE key=?',(key,)): rows.append([b.btn('↩️ Вернуть стандартный',f'ap:tr:{key}')])
    return f'<b>{label}</b>\nВставки: {b.e(holders) or "нет"}\n\nТекущий текст:\n{await b.get_text(key)}',b.markup(*rows,back('ap:txt'))
async def destinations(report=''):
    lines=['🛠 <b>Группы и темы</b>','']
    for kind,label in (('payments','Оплаты и подтверждение заказов'),('orders','Заказы и опросы')):
        chat,thread=await b.get_dest(kind); source='задано командой' if await b.get_setting(f'{kind}_chat') else 'из переменных'
        lines.append(f'{label}: чат <code>{chat}</code>, тема <code>{thread or "общая"}</code> ({source})')
    lines+=['','Отправьте внутри нужной темы:','<code>/set_payments</code> — оплаты','<code>/set_orders</code> — заказы',report]
    return '\n'.join(lines),b.markup([b.btn('🔍 Проверить отправку','ap:dtest')],back())
async def broadcast_view():
    rows=[[b.btn(f'{label.capitalize()} ({len(await b.audience_ids(k))})',f'ap:ba:{k}')] for k,(label,_) in b.AUDIENCES.items()]
    return '📣 <b>Рассылка</b>\n\nВыберите аудиторию.'+('\nУже идёт другая рассылка.' if b.broadcast['running'] else ''),b.markup(*rows,back())

@b.admin_router.message(Command('admin'))
async def command(message: Message,state: FSMContext):
    await state.clear(); text,kb=await home(); await message.answer(text,reply_markup=kb)
@b.admin_router.message(CommandStart())
async def start(message: Message,state: FSMContext,command: CommandObject):
    # Admin router runs before the customer router: preserve checkout payloads.
    from handlers import start as customer_start
    await customer_start(message, state, command)
@b.admin_router.callback_query(F.data.startswith('ap:'))
async def panel(cb: CallbackQuery,state: FSMContext,bot: Bot):
    data=await state.get_data(); await state.clear()
    parts=cb.data.split(':'); act=parts[1]; arg=parts[2] if len(parts)>2 else None
    if act=='home': view=await home()
    elif act=='tog':
        await b.set_setting('orders_open','0' if await b.orders_open() else '1'); view=await home()
    elif act=='st': view=await stats(arg or 'today')
    elif act=='ord': view=await orders(arg)
    elif act=='o': view=await card(int(arg))
    elif act=='pos': view=await positions()
    elif act=='p': view=await position(int(arg))
    elif act=='pe':
        await state.set_state(b.Admin.edit_price); await state.update_data(qty=int(arg))
        view=(f'Введите цену для {arg}: звёзды рубли. Например: <code>30 45</code>',b.markup(back(f'ap:p:{arg}')))
    elif act=='pd': view=(f'Удалить позицию {arg}?',b.markup([b.btn('🗑 Да, удалить',f'ap:pdy:{arg}')],back(f'ap:p:{arg}')))
    elif act=='pdy':
        await b.db.execute('DELETE FROM positions WHERE quantity=?',(int(arg),)); await b.db.commit(); view=await positions()
    elif act=='pa':
        await state.set_state(b.Admin.add_qty); view=('Введите количество новой позиции:',b.markup(back('ap:pos')))
    elif act=='pv':
        await b.db.execute('UPDATE positions SET in_menu=1-in_menu WHERE quantity=?',(int(arg),)); await b.db.commit(); view=await position(int(arg))
    elif act=='menu': view=await menu()
    elif act in ('mcols','mmax','mcust'):
        cols,mx,custom=await b.menu_settings()
        key,value={'mcols':('menu_cols',str(cols%4+1)),'mmax':('btn_max','0' if mx else '1'),'mcust':('btn_custom','0' if custom else '1')}[act]
        await b.set_setting(key,value); view=await menu()
    elif act=='mall':
        await b.db.execute('UPDATE positions SET in_menu=?',(1 if arg=='1' else 0,)); await b.db.commit(); view=await menu()
    elif act=='mprev':
        pos=await b.get_positions()
        if not pos: await cb.answer('Позиций пока нет',show_alert=True); return
        await cb.message.answer(await b.get_text('start_text',min=pos[0][0],max=pos[-1][0]),reply_markup=await b.qty_kb(await b.get_menu_positions())); await cb.answer(); return
    elif act=='txt': view=await texts()
    elif act=='t': view=await text_view(arg)
    elif act=='te':
        await state.set_state(b.Admin.edit_text); await state.update_data(key=arg)
        view=(f'Отправьте новый текст. Форматирование сохранится.\nВставки: {b.e(b.DEFAULT_TEXTS[arg][2]) or "нет"}',b.markup(back(f'ap:t:{arg}')))
    elif act=='tr':
        await b.db.execute('DELETE FROM texts WHERE key=?',(arg,)); await b.db.commit(); view=await text_view(arg)
    elif act=='dest': view=await destinations()
    elif act=='dtest':
        reports=[]
        for kind in ('payments','orders'):
            chat,thread=await b.get_dest(kind)
            try: await bot.send_message(chat,'Проверка отправки: '+kind,message_thread_id=thread); reports.append(kind+': доставлено')
            except TelegramAPIError as exc: reports.append(kind+': '+b.e(exc.message))
        view=await destinations('\n'.join(reports))
    elif act=='bc': view=await broadcast_view()
    elif act=='ba':
        if arg not in b.AUDIENCES: view=await broadcast_view()
        else:
            await state.set_state(b.Admin.broadcast_msg); await state.update_data(audience=arg)
            view=(f'Рассылка {b.AUDIENCES[arg][0]}. Получателей: {len(await b.audience_ids(arg))}.\nОтправьте текст, фото или видео для рассылки.',b.markup(back('ap:bc')))
    elif act=='bgo':
        if not data.get('src_msg'): await cb.answer('Рассылка уже запущена или отменена',show_alert=True); return
        if b.broadcast['running']: await cb.answer('Другая рассылка ещё идёт',show_alert=True); return
        ids=await b.audience_ids(data['audience']); b.broadcast.update(running=True,stop=False)
        task=asyncio.create_task(run_broadcast(bot,cb.message.chat.id,ids,data['src_chat'],data['src_msg']))
        b.background_tasks.add(task); task.add_done_callback(b.background_tasks.discard)
        view=('Рассылка запущена. Ход выполнения — ниже.',b.markup(back()))
    elif act=='bstop': b.broadcast['stop']=True; await cb.answer('Останавливаю…'); return
    else: view=await home()
    await show(cb,view); await cb.answer()

@b.admin_router.message(b.Admin.add_qty,F.text)
async def add_qty(message: Message,state: FSMContext):
    text=message.text.strip()
    if not text.isdigit() or len(text)>7 or not 1<=int(text)<=1000000:
        await message.answer('Нужно целое число от 1 до 1000000.'); return
    if await b.get_position(int(text)):
        await message.answer('Такая позиция уже есть. Введите другое количество.'); return
    await state.update_data(qty=int(text)); await state.set_state(b.Admin.add_price)
    await message.answer('Введите цену: звёзды рубли, например <code>45 65</code>',reply_markup=b.markup(back('ap:pos')))
@b.admin_router.message(b.Admin.add_price,F.text)
@b.admin_router.message(b.Admin.edit_price,F.text)
async def set_price(message: Message,state: FSMContext):
    p=price(message.text)
    if not p: await message.answer('Нужно два положительных числа: звёзды рубли.'); return
    qty=(await state.get_data())['qty']; current=await state.get_state()
    if current==b.Admin.add_price.state:
        await b.db.execute('INSERT INTO positions (quantity,stars,rub,in_storefront,sort_order) VALUES (?,?,?,1,?) ON CONFLICT(quantity) DO UPDATE SET stars=excluded.stars,rub=excluded.rub',(qty,*p,qty))
    else: await b.db.execute('UPDATE positions SET stars=?,rub=? WHERE quantity=?',(*p,qty))
    await b.db.commit(); await state.clear()
    await message.answer(f'✅ Цена позиции {qty} сохранена: {p[0]}⭐ / {p[1]}₽',reply_markup=b.markup(back('ap:pos')))
@b.admin_router.message(b.Admin.edit_text,F.text)
async def set_text(message: Message,state: FSMContext):
    if len(message.html_text)>3500: await message.answer('Слишком длинный текст. Сократите до 3500 символов.'); return
    key=(await state.get_data())['key']
    await b.db.execute('INSERT OR REPLACE INTO texts VALUES (?,?)',(key,message.html_text)); await b.db.commit(); await state.clear()
    await message.answer('✅ Текст сохранён.',reply_markup=b.markup(back(f'ap:t:{key}')))
@b.admin_router.message(b.Admin.broadcast_msg)
async def broadcast_message(message: Message,state: FSMContext):
    if message.text and message.text.startswith('/'):
        await state.clear(); await message.answer('Рассылка отменена: команду отправляйте отдельно.'); return
    aud=(await state.get_data())['audience']; count=len(await b.audience_ids(aud))
    if not count: await state.clear(); await message.answer('В этой аудитории нет получателей.'); return
    await state.update_data(src_chat=message.chat.id,src_msg=message.message_id); await state.set_state(b.Admin.broadcast_confirm)
    await message.reply(f'Отправить {b.AUDIENCES[aud][0]}? Получателей: {count}.',reply_markup=b.markup([b.btn('✅ Отправить','ap:bgo')],back('ap:bc')))
async def run_broadcast(bot,admin_chat,ids,src_chat,src_msg):
    sent=blocked=failed=0; stopped=False; status=None
    stop_kb=b.markup([b.btn('🛑 Остановить','ap:bstop')])
    try:
        status=await bot.send_message(admin_chat,f'📣 Рассылка: 0/{len(ids)}',reply_markup=stop_kb)
        for i,uid in enumerate(ids,1):
            if b.broadcast['stop']: stopped=True; break
            try:
                try: await bot.copy_message(uid,src_chat,src_msg)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after+1); await bot.copy_message(uid,src_chat,src_msg)
                sent+=1
            except TelegramForbiddenError:
                blocked+=1; await b.db.execute('UPDATE users SET blocked=1 WHERE user_id=?',(uid,)); await b.db.commit()
            except TelegramAPIError: failed+=1
            if i%20==0:
                try: await status.edit_text(f'📣 Рассылка: {i}/{len(ids)}',reply_markup=stop_kb)
                except TelegramAPIError: pass
            await asyncio.sleep(0.05)
    except TelegramAPIError:
        logging.exception('Ошибка рассылки'); failed+=1
    finally: b.broadcast.update(running=False,stop=False)
    report=f"Рассылка {'остановлена' if stopped else 'завершена'}\nДоставлено: {sent} из {len(ids)}\nЗаблокировали: {blocked}\nДругие ошибки: {failed}"
    try:
        if status: await status.edit_text(report,reply_markup=b.markup(back()))
        else: await bot.send_message(admin_chat,report)
    except TelegramAPIError: logging.exception('Не удалось отправить отчёт о рассылке')

import storefront_admin
