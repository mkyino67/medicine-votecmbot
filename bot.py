"""Shared configuration and SQLite storage; Telegram handlers live in handlers.py."""
import asyncio
import html
import logging
import os
import re
from pathlib import Path
import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
PAYMENTS_CHAT_ID = int(os.getenv('PAYMENTS_CHAT_ID','-1004477091535'))
ORDERS_CHAT_ID = int(os.getenv('ORDERS_CHAT_ID','-1003889093155'))
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS','6615382200,8221162959').split(',') if x.strip()}
PAYMENT_DETAILS = os.getenv('PAYMENT_DETAILS','Реквизиты не указаны').replace('\\n','\n')
DB_PATH = os.getenv('DB_PATH','/data/orders.db')
UTC_OFFSET_HOURS = int(os.getenv('UTC_OFFSET_HOURS','3'))
def optional_int(name,default=''):
    value = os.getenv(name,default).strip()
    return int(value) if value else None
PAYMENTS_THREAD_ID = optional_int('PAYMENTS_THREAD_ID')
ORDERS_THREAD_ID = optional_int('ORDERS_THREAD_ID','7')
if PAYMENTS_THREAD_ID == 1: PAYMENTS_THREAD_ID = None
DEFAULT_POSITIONS = {60:(15,25),100:(30,45),160:(45,65),200:(70,95),260:(80,110),300:(110,145),360:(120,160),400:(130,180),460:(150,205),500:(165,225),560:(180,240),600:(200,265),660:(215,280),700:(225,300),760:(240,315),800:(265,350)}
DEFAULT_TEXTS = {
'start_text':('Выбор количества','Выберите количество (от {min} до {max}):','{min}, {max}'),
'closed_text':('Приём приостановлен','⏸ Приём заказов временно приостановлен. Загляните позже!',''),
'ask_link':('Ссылка','🔗 Отправьте ссылку на пост (например, https://t.me/channel/12):',''),
'ask_username':('Юзернейм','💬 Отправьте юзернейм (например, @username):',''),
'review_text':('Заказ на проверке','🧾 Заказ #{order_id} отправлен на проверку. Как только его примут, я пришлю реквизиты для оплаты.','{order_id}'),
'declined_text':('Заказ не принят','❌ Заказ #{order_id} не принят. Возможно, на эту публикацию уже идёт заказ. Свяжитесь с продавцом.','{order_id}'),
'payment_text':('Реквизиты','✅ Заказ #{order_id} принят.\n\nК оплате: {rub}₽ ({stars}⭐)\n\n'+html.escape(PAYMENT_DETAILS),'{order_id}, {rub}, {stars}'),
'payment_sent':('Скриншот отправлен','Скриншот по заказу #{order_id} отправлен на проверку. Ожидайте подтверждения.','{order_id}'),
'approved_text':('Оплата подтверждена','✅ Оплата по заказу #{order_id} подтверждена. Заказ принят в работу.','{order_id}'),
'rejected_text':('Оплата отклонена','❌ Оплата по заказу #{order_id} не подтверждена. Если это ошибка — свяжитесь с продавцом.','{order_id}'),
'done_text':('Заказ выполнен','✅ Ваш заказ #{order_id} выполнен: оттапано {tapped} из {quantity}.','{order_id}, {tapped}, {quantity}')}
POLL_OPTIONS = ['20','40','60','80','100','тапали']
POLL_VALUES = [20,40,60,80,100,0]
LINK_RE = re.compile(r'^https?://t\.me/(c/\d+|[A-Za-z0-9_]{4,})(/\d+){1,2}/?(\?\S*)?$')
USERNAME_RE = re.compile(r'^@?[A-Za-z0-9_]{5,32}$')
id_router, admin_router, router = Router(), Router(), Router()
admin_router.message.filter(F.chat.type == 'private',F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
router.message.filter(F.chat.type == 'private')
db: aiosqlite.Connection
SCHEMA = '''
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT,buyer_id INTEGER NOT NULL,buyer_username TEXT,quantity INTEGER NOT NULL,link TEXT NOT NULL,target_username TEXT NOT NULL,price_stars INTEGER NOT NULL,price_rub INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'awaiting_payment',payment_msg_id INTEGER,poll_msg_id INTEGER,poll_id TEXT,tapped INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP,approved_at TEXT,pay_method TEXT,payments_chat INTEGER,payments_thread INTEGER,orders_chat INTEGER,orders_thread INTEGER);
CREATE TABLE IF NOT EXISTS votes (poll_id TEXT NOT NULL,user_id INTEGER NOT NULL,option_id INTEGER NOT NULL,PRIMARY KEY(poll_id,user_id));
CREATE TABLE IF NOT EXISTS positions (quantity INTEGER PRIMARY KEY,stars INTEGER NOT NULL,rub INTEGER NOT NULL,in_menu INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS texts (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY,username TEXT,first_name TEXT,first_seen TEXT DEFAULT CURRENT_TIMESTAMP,last_seen TEXT DEFAULT CURRENT_TIMESTAMP,blocked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''
class Order(StatesGroup):
    custom_qty,link,username,confirm = State(),State(),State(),State()
class Admin(StatesGroup):
    add_qty,add_price,edit_price,edit_text,broadcast_msg,broadcast_confirm = State(),State(),State(),State(),State(),State()
def e(value): return html.escape(str(value))
def btn(text,data):
    from button_editor import render
    return render(text,data)
def markup(*rows): return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])
async def get_order(oid):
    cur = await db.execute('SELECT * FROM orders WHERE id=?',(oid,))
    return await cur.fetchone()
async def num(sql,params=()):
    cur = await db.execute(sql,params); row = await cur.fetchone()
    return (row[0] or 0) if row else 0
async def poll_total(pid):
    cur = await db.execute('SELECT option_id,COUNT(*) FROM votes WHERE poll_id=? GROUP BY option_id',(pid,))
    return sum(POLL_VALUES[o]*n for o,n in await cur.fetchall() if 0<=o<len(POLL_VALUES))
async def get_positions(menu=False):
    cur = await db.execute('SELECT quantity,stars,rub FROM positions '+('WHERE in_menu=1 ' if menu else '')+'ORDER BY quantity')
    return [(r['quantity'],r['stars'],r['rub']) for r in await cur.fetchall()]
async def get_menu_positions(): return await get_positions(True)
async def get_position(q):
    cur = await db.execute('SELECT stars,rub FROM positions WHERE quantity=?',(q,)); r = await cur.fetchone()
    return (r['stars'],r['rub']) if r else None
async def get_setting(key,default=''):
    cur = await db.execute('SELECT value FROM settings WHERE key=?',(key,)); r = await cur.fetchone()
    return r['value'] if r else default
async def set_setting(key,value):
    await db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,value)); await db.commit()
async def orders_open(): return await get_setting('orders_open','1')=='1'
async def menu_settings():
    return min(max(int(await get_setting('menu_cols','3') or 3),1),4),await get_setting('btn_max','1')=='1',await get_setting('btn_custom','1')=='1'
async def get_text(key,**values):
    cur = await db.execute('SELECT value FROM texts WHERE key=?',(key,)); row = await cur.fetchone()
    text = row['value'] if row else DEFAULT_TEXTS[key][1]
    for k,v in values.items(): text=text.replace('{'+k+'}',str(v))
    return text
async def qty_kb(positions):
    cols,mx,custom = await menu_settings()
    buttons = [btn(str(q),f'qty:{q}') for q,_,_ in positions]
    rows = [buttons[i:i+cols] for i in range(0,len(buttons),cols)]
    if not rows and not mx and not custom: custom=True
    if mx: rows.append([btn('Максимальное количество','qty:max')])
    if custom: rows.append([btn('✍️ Ввести своё количество','qty:custom')])
    return markup(*rows)
async def send_start(message):
    if not await orders_open() and message.chat.id not in ADMIN_IDS:
        await message.answer(await get_text('closed_text')); return
    pos = await get_positions()
    if not pos: await message.answer('Сейчас приём заказов закрыт.'); return
    await message.answer(await get_text('start_text',min=pos[0][0],max=pos[-1][0]),reply_markup=await qty_kb(await get_menu_positions()))
async def get_dest(kind):
    chat = await get_setting(f'{kind}_chat')
    if chat:
        thread = await get_setting(f'{kind}_thread')
        return int(chat),int(thread) if thread else None
    return (PAYMENTS_CHAT_ID,PAYMENTS_THREAD_ID) if kind=='payments' else (ORDERS_CHAT_ID,ORDERS_THREAD_ID)
async def order_dest(order,kind):
    return (order[f'{kind}_chat'],order[f'{kind}_thread']) if order[f'{kind}_chat'] else await get_dest(kind)
def norm_link(link): return re.sub(r'^https?://','',link.strip().lower().split('?')[0].rstrip('/'))
ACTIVE_STATUSES=('pending_approval','awaiting_payment','pending_review','in_progress')
STATUS_LABELS={'pending_approval':'🆕 ждёт принятия','awaiting_payment':'🕓 ждёт оплаты','pending_review':'⏳ ждёт проверки оплаты','in_progress':'🔄 в работе','closed':'🔒 опрос закрыт','delivered':'✅ выполнен','rejected':'❌ оплата отклонена','declined':'🚫 не принят'}
PAID="status IN ('in_progress','closed','delivered')"
PERIODS=[('today','Сегодня'),('7d','7 дней'),('30d','30 дней'),('all','Всё время')]
AUDIENCES={'all':('всем','1=1'),'buyers':('покупателям',f'user_id IN (SELECT buyer_id FROM orders WHERE {PAID})'),'active':('активным за 30 дней',"last_seen>=datetime('now','-30 days')")}
broadcast={'running':False,'stop':False}
background_tasks=set()
def in_period(col,p):
    if p=='today': return f"date({col},'{UTC_OFFSET_HOURS:+d} hours')=date('now','{UTC_OFFSET_HOURS:+d} hours')"
    if p in ('7d','30d'): return f"{col}>=datetime('now','-{7 if p=='7d' else 30} days')"
    return '1=1'
async def compute_stats(p):
    pc,pp=in_period('created_at',p),in_period('approved_at',p)
    queries={'created':f'SELECT COUNT(*) FROM orders WHERE {pc}','paid':f'SELECT COUNT(*) FROM orders WHERE {PAID} AND {pp}','rejected':f"SELECT COUNT(*) FROM orders WHERE status IN ('rejected','declined') AND {pc}",'sold':f'SELECT SUM(quantity) FROM orders WHERE {PAID} AND {pp}','tapped':f"SELECT SUM(tapped) FROM orders WHERE status IN ('closed','delivered') AND {pp}",'unknown':f'SELECT COUNT(*) FROM orders WHERE {PAID} AND pay_method IS NULL AND {pp}','users_total':'SELECT COUNT(*) FROM users','users_blocked':'SELECT COUNT(*) FROM users WHERE blocked=1','users_new':f"SELECT COUNT(*) FROM users WHERE {in_period('first_seen',p)}",'users_active':f"SELECT COUNT(*) FROM users WHERE {in_period('last_seen',p)}",'buyers':f'SELECT COUNT(DISTINCT buyer_id) FROM orders WHERE {PAID} AND {pp}'}
    for method,col in (('rub','price_rub'),('stars','price_stars')):
        where=f"{PAID} AND pay_method='{method}' AND {pp}"
        queries[method]=f'SELECT SUM({col}) FROM orders WHERE {where}'
        queries[method+'_cnt']=f'SELECT COUNT(*) FROM orders WHERE {where}'
    for key,status in (('pending','pending_review'),('in_progress','in_progress'),('awaiting','awaiting_payment'),('approval','pending_approval')):
        queries[key]=f"SELECT COUNT(*) FROM orders WHERE status='{status}'"
    stats={k:await num(sql) for k,sql in queries.items()}
    cur=await db.execute(f'SELECT quantity,COUNT(*) AS c FROM orders WHERE {PAID} AND {pp} GROUP BY quantity ORDER BY c DESC,quantity LIMIT 3')
    stats['top']=[(r['quantity'],r['c']) for r in await cur.fetchall()]
    return stats
async def audience_ids(aud):
    cur=await db.execute(f'SELECT user_id FROM users WHERE blocked=0 AND {AUDIENCES[aud][1]} ORDER BY user_id')
    return [r['user_id'] for r in await cur.fetchall()]
class TrackUsers(BaseMiddleware):
    async def __call__(self,handler,event,data):
        user=None
        if event.message and event.message.chat.type=='private': user=event.message.from_user
        elif event.callback_query and event.callback_query.message and event.callback_query.message.chat.type=='private': user=event.callback_query.from_user
        if user and not user.is_bot:
            try:
                await db.execute('INSERT INTO users (user_id,username,first_name) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_seen=CURRENT_TIMESTAMP,blocked=0',(user.id,user.username,user.first_name)); await db.commit()
            except Exception: logging.exception('Не удалось сохранить пользователя %s',user.id)
        return await handler(event,data)
async def init_db():
    await db.executescript(SCHEMA)
    cur=await db.execute('PRAGMA table_info(orders)'); cols={r['name'] for r in await cur.fetchall()}
    for col,typ in (('approved_at','TEXT'),('pay_method','TEXT'),('payments_chat','INTEGER'),('payments_thread','INTEGER'),('orders_chat','INTEGER'),('orders_thread','INTEGER')):
        if col not in cols: await db.execute(f'ALTER TABLE orders ADD COLUMN {col} {typ}')
    cur=await db.execute('PRAGMA table_info(positions)')
    if 'in_menu' not in {r['name'] for r in await cur.fetchall()}: await db.execute('ALTER TABLE positions ADD COLUMN in_menu INTEGER NOT NULL DEFAULT 1')
    cur=await db.execute('PRAGMA table_info(positions)')
    fields={r['name'] for r in await cur.fetchall()}
    for name, definition in (('title', "TEXT NOT NULL DEFAULT ''"), ('description', "TEXT NOT NULL DEFAULT ''"), ('in_storefront', 'INTEGER NOT NULL DEFAULT 0'), ('sort_order', 'INTEGER NOT NULL DEFAULT 0')):
        if name not in fields: await db.execute(f'ALTER TABLE positions ADD COLUMN {name} {definition}')
    await db.execute("UPDATE orders SET approved_at=created_at WHERE approved_at IS NULL AND status IN ('in_progress','closed','delivered')")
    await db.execute('INSERT OR IGNORE INTO users (user_id,username,first_seen,last_seen) SELECT buyer_id,MAX(buyer_username),MIN(created_at),MAX(created_at) FROM orders GROUP BY buyer_id')
    if await num('PRAGMA user_version')==0:
        await db.executemany('INSERT OR IGNORE INTO positions (quantity,stars,rub) VALUES (?,?,?)',[(q,s,r) for q,(s,r) in DEFAULT_POSITIONS.items()])
        await db.execute('PRAGMA user_version=1')
    # Apply the owner's storefront price list once; keep later admin edits.
    if not await get_setting('storefront_prices_v1'):
        await db.executemany(
            'INSERT INTO positions (quantity,stars,rub) VALUES (?,?,?) '
            'ON CONFLICT(quantity) DO UPDATE SET stars=excluded.stars,rub=excluded.rub',
            [(60,15,25),(80,25,40),(100,30,50),(140,40,65),(160,50,75),(200,80,95)])
        await db.execute("INSERT INTO settings (key,value) VALUES ('storefront_prices_v1','1')")
    if not await get_setting('storefront_catalog_v1'):
        await db.execute('UPDATE positions SET sort_order=quantity, in_storefront=CASE WHEN quantity IN (60,80,100,140,160,200) THEN 1 ELSE 0 END')
        await db.execute("INSERT INTO settings (key,value) VALUES ('storefront_catalog_v1','1')")
    await db.commit()
    from button_editor import load
    await load()
async def main():
    global db
    if not BOT_TOKEN: raise RuntimeError('Set secret runtime variable BOT_TOKEN')
    if not ADMIN_IDS: raise RuntimeError('ADMIN_IDS must not be empty')
    import handlers
    import admin
    Path(DB_PATH).parent.mkdir(parents=True,exist_ok=True)
    db=await aiosqlite.connect(DB_PATH); db.row_factory=aiosqlite.Row
    bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await init_db()
        dp=Dispatcher(storage=MemoryStorage()); dp.update.outer_middleware(TrackUsers())
        dp.include_routers(id_router,admin_router,router)
        await dp.start_polling(bot,handle_signals=False)
    finally:
        for task in background_tasks: task.cancel()
        await asyncio.gather(*background_tasks,return_exceptions=True)
        await bot.session.close(); await db.close()
