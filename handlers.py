"""Customer flow, moderation, polls, and group routing."""
import logging
from aiogram import Bot, F
from aiogram.filters import Command, CommandStart, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, PollAnswer, ChatMemberUpdated, ReplyParameters
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramBadRequest
import bot as b

@b.id_router.message(Command('id'))
async def cmd_id(message: Message):
    thread=message.message_thread_id if (message.is_topic_message or message.chat.is_forum) else None
    await message.reply(f'ID чата: <code>{message.chat.id}</code>\nID темы: <code>{thread or "нет (общая тема)"}</code>')

@b.id_router.message(Command('set_orders','set_payments'))
async def set_destination(message: Message):
    if message.chat.type not in ('group','supergroup'):
        await message.reply('Отправьте команду в группе, внутри нужной темы.'); return
    if not message.from_user or message.from_user.id not in b.ADMIN_IDS: return
    kind='orders' if 'set_orders' in message.text.split()[0] else 'payments'
    thread=message.message_thread_id if (message.is_topic_message or message.chat.is_forum) else None
    if thread==1: thread=None
    await b.set_setting(f'{kind}_chat',str(message.chat.id))
    await b.set_setting(f'{kind}_thread',str(thread) if thread else '')
    await message.reply(f'Место сохранено: чат <code>{message.chat.id}</code>, тема <code>{thread or "общая"}</code>')

def approval_kb(oid): return b.markup([b.btn('✅ Принять',f'adm:accept:{oid}'),b.btn('❌ Отклонить',f'adm:decline:{oid}')])
def review_kb(oid): return b.markup([b.btn('✅ Оплата в ₽',f'adm:approve:{oid}:rub'),b.btn('✅ Оплата в ⭐',f'adm:approve:{oid}:stars')],[b.btn('❌ Отклонить',f'adm:reject:{oid}')])
def progress_kb(oid,close=True):
    rows=[[b.btn('📣 Сообщить о выполнении',f'adm:notify:{oid}')]]
    if close: rows.append([b.btn('🔒 Закрыть опрос',f'adm:close:{oid}')])
    return b.markup(*rows)
def payment_caption(o,note=None):
    buyer='@'+b.e(o['buyer_username']) if o['buyer_username'] else f"id{o['buyer_id']}"
    text=f"💳 Скриншот оплаты\n\n🧾 Заказ #{o['id']}\n👤 {buyer}\n📦 Количество: {o['quantity']}\n🔗 Ссылка: {b.e(o['link'])}\n💬 Юзернейм: {b.e(o['target_username'])}\n💰 Цена: {o['price_stars']}⭐ / {o['price_rub']}₽"
    return text+('\n\n'+note if note else '')
def admin_name(user): return '@'+b.e(user.username) if user.username else b.e(user.full_name)
async def approval_text(o,note=None):
    buyer='@'+b.e(o['buyer_username']) if o['buyer_username'] else f"id{o['buyer_id']}"
    text=f"🆕 <b>Новый заказ #{o['id']}</b> — на подтверждение\n\n👤 {buyer}\n📦 Количество: {o['quantity']}\n🔗 Ссылка: {b.e(o['link'])}\n💬 Юзернейм: {b.e(o['target_username'])}\n💰 Цена: {o['price_stars']}⭐ / {o['price_rub']}₽"
    if note: return text+'\n\n'+note
    marks=','.join('?'*len(b.ACTIVE_STATUSES))
    cur=await b.db.execute(f'SELECT id,link,target_username,status,quantity FROM orders WHERE status IN ({marks}) AND id!=?',(*b.ACTIVE_STATUSES,o['id']))
    dups=[r for r in await cur.fetchall() if b.norm_link(r['link'])==b.norm_link(o['link'])]
    if not dups: return text+'\n\n✅ Активных заказов на эту ссылку нет.'
    text+='\n\n⚠️ <b>На эту ссылку уже есть активные заказы:</b>'
    for d in dups: text+=f"\n• #{d['id']} — {b.STATUS_LABELS.get(d['status'],d['status'])}, {d['quantity']} → {b.e(d['target_username'])}"
    return text
async def notify_admins(bot,text,reply_markup=None):
    for aid in b.ADMIN_IDS:
        try: await bot.send_message(aid,text,reply_markup=reply_markup)
        except TelegramAPIError: logging.warning('Не удалось написать админу %s',aid)
async def safe_notify(bot,uid,text):
    try: await bot.send_message(uid,text); return True
    except (TelegramForbiddenError,TelegramBadRequest):
        logging.warning('Не удалось написать пользователю %s',uid); return False

@b.router.message(CommandStart())
async def start(message: Message,state: FSMContext,command: CommandObject):
    # Only a delivered /start in a private chat may begin checkout.
    if message.chat.type != 'private':
        return
    await state.clear()
    payload = command.args or ''
    if payload.startswith('order_'):
        if not await b.orders_open() and message.from_user.id not in b.ADMIN_IDS:
            await message.answer(await b.get_text('closed_text'))
            return
        raw = payload.removeprefix('order_')
        if not raw.isascii() or not raw.isdigit() or len(raw) > 7:
            await message.answer('Некорректная ссылка на товар. Выберите пакет в меню.')
            await b.send_start(message)
            return
        qty = int(raw)
        price = await b.get_position(qty)
        if not price:
            await message.answer('Этот пакет больше недоступен. Выберите другой в меню.')
            await b.send_start(message)
            return
        await state.update_data(quantity=qty)
        await state.set_state(b.Order.link)
        await message.answer(f'Вы выбрали {qty} тапов. Стоимость: {price[0]} звёзд / {price[1]} ₽.\n\n' + await b.get_text('ask_link'))
        return
    await b.send_start(message)
@b.router.callback_query(F.data.startswith('qty:'))
async def quantity(cb: CallbackQuery,state: FSMContext):
    if not await b.orders_open() and cb.from_user.id not in b.ADMIN_IDS:
        await cb.message.answer(await b.get_text('closed_text')); await cb.answer(); return
    pos=await b.get_positions()
    if not pos: await cb.answer('Сейчас приём заказов закрыт',show_alert=True); return
    value=cb.data.split(':')[1]
    if value=='custom':
        await state.set_state(b.Order.custom_qty)
        await cb.message.answer(f'Введите количество из прайса (от {pos[0][0]} до {pos[-1][0]}):'); await cb.answer(); return
    qty=((await b.get_menu_positions()) or pos)[-1][0] if value=='max' else int(value)
    if not await b.get_position(qty): await cb.answer('Эта позиция больше недоступна',show_alert=True); return
    await state.update_data(quantity=qty); await state.set_state(b.Order.link)
    await cb.message.answer(await b.get_text('ask_link')); await cb.answer()
@b.router.message(b.Order.custom_qty,F.text)
async def custom(message: Message,state: FSMContext):
    text=message.text.strip()
    if not text.isdigit() or len(text)>7 or not await b.get_position(int(text)):
        available=', '.join(str(q) for q,_,_ in await b.get_positions())
        await message.answer(f'Такого количества нет в прайсе. Доступно: {available}.\nПопробуйте ещё раз:'); return
    await state.update_data(quantity=int(text)); await state.set_state(b.Order.link)
    await message.answer(await b.get_text('ask_link'))
@b.router.message(b.Order.link,F.text)
async def link(message: Message,state: FSMContext):
    text=message.text.strip()
    if len(text)>256 or not b.LINK_RE.match(text):
        await message.answer('Это не похоже на ссылку на пост. Формат: https://t.me/channel/12'); return
    await state.update_data(link=text); await state.set_state(b.Order.username)
    await message.answer(await b.get_text('ask_username'))
@b.router.message(b.Order.username,F.text)
async def username(message: Message,state: FSMContext):
    raw=message.text.strip()
    if not b.USERNAME_RE.match(raw):
        await message.answer('Юзернейм: @username (латиница, цифры, _, от 5 до 32 символов).'); return
    target='@'+raw.lstrip('@'); data=await state.get_data(); price=await b.get_position(data['quantity'])
    if not price:
        await state.clear(); await message.answer('Позиция недоступна. Нажмите /start'); return
    stars,rub=price
    await state.update_data(target=target,stars=stars,rub=rub); await state.set_state(b.Order.confirm)
    kb=b.markup([b.btn('✅ Всё верно','confirm:yes')],[b.btn('✏️ Изменить','confirm:edit'),b.btn('❌ Отменить','confirm:cancel')])
    await message.answer(f"Проверьте данные:\n\n📦 Количество: {data['quantity']}\n🔗 Ссылка: {b.e(data['link'])}\n💬 Юзернейм: {b.e(target)}\n💰 Цена: {stars}⭐ / {rub}₽\n\nВсё верно?",reply_markup=kb)
@b.router.callback_query(b.Order.confirm,F.data.startswith('confirm:'))
async def confirm(cb: CallbackQuery,state: FSMContext,bot: Bot):
    action=cb.data.split(':')[1]; await cb.message.edit_reply_markup(reply_markup=None)
    if action=='cancel':
        await state.clear(); await cb.message.answer('Заказ отменён. Нажмите /start, чтобы начать заново.'); await cb.answer(); return
    if action=='edit': await state.clear(); await b.send_start(cb.message); await cb.answer(); return
    if not await b.orders_open() and cb.from_user.id not in b.ADMIN_IDS:
        await state.clear(); await cb.message.answer(await b.get_text('closed_text')); await cb.answer(); return
    data=await state.get_data()
    if not all(k in data for k in ('quantity','link','target','stars','rub')):
        await cb.message.answer('Сессия устарела, начните заново: /start'); await cb.answer(); return
    cur=await b.db.execute("INSERT INTO orders (buyer_id,buyer_username,quantity,link,target_username,price_stars,price_rub,status) VALUES (?,?,?,?,?,?,?,'pending_approval')",(cb.from_user.id,cb.from_user.username,data['quantity'],data['link'],data['target'],data['stars'],data['rub']))
    await b.db.commit(); oid=cur.lastrowid; await state.clear()
    await cb.message.answer(await b.get_text('review_text',order_id=oid))
    o=await b.get_order(oid); text=await approval_text(o); chat,thread=await b.get_dest('payments')
    try: await bot.send_message(chat,text,reply_markup=approval_kb(oid),message_thread_id=thread)
    except TelegramAPIError:
        logging.exception('Не удалось отправить заказ на проверку')
        await notify_admins(bot,'Не удалось отправить заказ в группу. Проверьте /admin → Группы и темы.\n\n'+text,approval_kb(oid))
    await cb.answer()
@b.router.message(F.photo)
async def screenshot(message: Message,bot: Bot):
    cur=await b.db.execute("SELECT * FROM orders WHERE buyer_id=? AND status='awaiting_payment' ORDER BY id DESC LIMIT 1",(message.from_user.id,)); o=await cur.fetchone()
    if not o:
        waiting=await b.num("SELECT COUNT(*) FROM orders WHERE buyer_id=? AND status='pending_approval'",(message.from_user.id,))
        await message.answer('Заказ ещё не принят, дождитесь реквизитов.' if waiting else 'Нет заказа, ожидающего оплаты. Нажмите /start'); return
    chat,thread=await b.get_dest('payments')
    try: sent=await bot.send_photo(chat,photo=message.photo[-1].file_id,caption=payment_caption(o),reply_markup=review_kb(o['id']),message_thread_id=thread)
    except TelegramAPIError:
        logging.exception('Не удалось отправить скриншот')
        await notify_admins(bot,f"Не удалось передать скриншот заказа #{o['id']}. Проверьте /admin → Группы и темы.")
        await message.answer('Не получилось передать скриншот. Продавец уже знает, попробуйте позже.'); return
    await b.db.execute("UPDATE orders SET status='pending_review',payment_msg_id=?,payments_chat=?,payments_thread=? WHERE id=?",(sent.message_id,chat,thread,o['id'])); await b.db.commit()
    await message.answer(await b.get_text('payment_sent',order_id=o['id']))

@b.router.callback_query(F.data.startswith('adm:'))
async def action(cb: CallbackQuery,bot: Bot):
    if cb.from_user.id not in b.ADMIN_IDS: await cb.answer('Нет доступа',show_alert=True); return
    parts=cb.data.split(':'); act,oid=parts[1],int(parts[2]); method=parts[3] if len(parts)>3 else None
    o=await b.get_order(oid)
    if not o: await cb.answer('Заказ не найден',show_alert=True); return
    if act in ('accept','decline'):
        status='awaiting_payment' if act=='accept' else 'declined'
        res=await b.db.execute("UPDATE orders SET status=? WHERE id=? AND status='pending_approval'",(status,oid)); await b.db.commit()
        if not res.rowcount: await cb.answer('Заказ уже обработан',show_alert=True); return
        key='payment_text' if act=='accept' else 'declined_text'
        sent=await safe_notify(bot,o['buyer_id'],await b.get_text(key,order_id=oid,rub=o['price_rub'],stars=o['price_stars']))
        note=f'{"✅ Принят" if act=="accept" else "❌ Отклонён"} ({admin_name(cb.from_user)})'
        if not sent: note+=' — не удалось написать покупателю'
        await cb.message.edit_text(await approval_text(o,note),reply_markup=None)
    elif act=='approve':
        res=await b.db.execute("UPDATE orders SET status='in_progress',approved_at=CURRENT_TIMESTAMP,pay_method=? WHERE id=? AND status='pending_review'",(method if method in ('rub','stars') else None,oid)); await b.db.commit()
        if not res.rowcount: await cb.answer('Заказ уже обработан',show_alert=True); return
        chat,thread=await b.get_dest('orders'); info=None
        try:
            info=await bot.send_message(chat,f"🔗 {b.e(o['link'])}\n💬 {b.e(o['target_username'])}\n📦 Количество: {o['quantity']}",message_thread_id=thread)
            poll=await bot.send_poll(chat,question='сколько тапнули?',options=b.POLL_OPTIONS,is_anonymous=False,allows_multiple_answers=False,message_thread_id=thread)
        except TelegramAPIError:
            logging.exception('Не удалось отправить заказ')
            if info:
                try: await info.delete()
                except TelegramAPIError: pass
            await b.db.execute("UPDATE orders SET status='pending_review',approved_at=NULL,pay_method=NULL WHERE id=?",(oid,)); await b.db.commit()
            await cb.answer('Не удалось отправить заказ. Проверьте Группы и темы.',show_alert=True); return
        await b.db.execute('UPDATE orders SET poll_msg_id=?,poll_id=?,orders_chat=?,orders_thread=? WHERE id=?',(poll.message_id,poll.poll.id,chat,thread,oid)); await b.db.commit()
        await cb.message.edit_caption(caption=payment_caption(o,f'✅ Одобрено ({admin_name(cb.from_user)}), способ: {b.e(method)}'),reply_markup=progress_kb(oid))
        await safe_notify(bot,o['buyer_id'],await b.get_text('approved_text',order_id=oid))
    elif act=='reject':
        res=await b.db.execute("UPDATE orders SET status='rejected' WHERE id=? AND status='pending_review'",(oid,)); await b.db.commit()
        if not res.rowcount: await cb.answer('Заказ уже обработан',show_alert=True); return
        await cb.message.edit_caption(caption=payment_caption(o,f'❌ Отклонено ({admin_name(cb.from_user)})'),reply_markup=None)
        await safe_notify(bot,o['buyer_id'],await b.get_text('rejected_text',order_id=oid))
    elif act=='close':
        done=await finish_order(bot,oid); await cb.answer('Опрос закрыт' if done else 'Опрос уже закрыт'); return
    elif act=='notify':
        if o['status']=='in_progress': await cb.answer('Сначала закройте опрос',show_alert=True); return
        res=await b.db.execute("UPDATE orders SET status='delivered' WHERE id=? AND status='closed'",(oid,)); await b.db.commit()
        if not res.rowcount: await cb.answer('Заказ уже обработан',show_alert=True); return
        sent=await safe_notify(bot,o['buyer_id'],await b.get_text('done_text',order_id=oid,tapped=o['tapped'],quantity=o['quantity']))
        if not sent:
            await b.db.execute("UPDATE orders SET status='closed' WHERE id=? AND status='delivered'",(oid,)); await b.db.commit()
            await cb.answer('Не удалось уведомить, попробуйте позже',show_alert=True); return
        await cb.message.edit_caption(caption=payment_caption(o,f'📬 Заказчик уведомлён ({admin_name(cb.from_user)})'),reply_markup=None)
    await cb.answer('Готово')

async def finish_order(bot,oid):
    res=await b.db.execute("UPDATE orders SET status='closed' WHERE id=? AND status='in_progress'",(oid,)); await b.db.commit()
    if not res.rowcount: return False
    o=await b.get_order(oid); tapped=min(await b.poll_total(o['poll_id']),o['quantity'])
    await b.db.execute('UPDATE orders SET tapped=? WHERE id=?',(tapped,oid)); await b.db.commit()
    oc,ot=await b.order_dest(o,'orders'); pc,pt=await b.order_dest(o,'payments')
    try: await bot.stop_poll(oc,o['poll_msg_id'])
    except TelegramAPIError: logging.warning('Не удалось остановить опрос заказа %s',oid)
    verb='выполнен' if tapped>=o['quantity'] else 'закрыт'
    for chat,thread,mid in ((oc,ot,o['poll_msg_id']),(pc,pt,o['payment_msg_id'])):
        try: await bot.send_message(chat,f"✅ Заказ #{oid} {verb}: оттапано {tapped} из {o['quantity']}",message_thread_id=thread,reply_parameters=ReplyParameters(message_id=mid))
        except TelegramAPIError: logging.exception('Не удалось отправить итог заказа %s',oid)
    try: await bot.edit_message_reply_markup(chat_id=pc,message_id=o['payment_msg_id'],reply_markup=progress_kb(oid,False))
    except TelegramAPIError: pass
    return True
@b.router.poll_answer()
async def poll_answer(answer: PollAnswer,bot: Bot):
    if answer.user is None: return
    cur=await b.db.execute("SELECT id,quantity FROM orders WHERE poll_id=? AND status='in_progress'",(answer.poll_id,)); o=await cur.fetchone()
    if not o: return
    if answer.option_ids:
        opt=answer.option_ids[0]
        if not 0<=opt<len(b.POLL_VALUES): return
        await b.db.execute('INSERT OR REPLACE INTO votes VALUES (?,?,?)',(answer.poll_id,answer.user.id,opt))
    else: await b.db.execute('DELETE FROM votes WHERE poll_id=? AND user_id=?',(answer.poll_id,answer.user.id))
    await b.db.commit()
    if await b.poll_total(answer.poll_id)>=o['quantity']: await finish_order(bot,o['id'])
@b.router.my_chat_member(F.chat.type=='private')
async def blocked(event: ChatMemberUpdated):
    await b.db.execute('UPDATE users SET blocked=? WHERE user_id=?',(int(event.new_chat_member.status in ('kicked','left')),event.chat.id)); await b.db.commit()
@b.router.message(F.text,StateFilter(None))
async def fallback(message: Message): await message.answer('Чтобы сделать заказ, нажмите /start')
