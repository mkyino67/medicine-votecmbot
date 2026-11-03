"""HTTP health service plus Telegram long polling. Sandbox uses PREVIEW_MODE=1."""
import asyncio
import logging
import os
from aiohttp import web
import bot

PREVIEW = os.getenv('PREVIEW_MODE', '0') == '1'

async def index(request):
    from pathlib import Path
    return web.Response(text=Path(__file__).with_name('storefront.html').read_text(), content_type='text/html')

async def catalog(request):
    try:
        cur=await bot.db.execute('SELECT quantity AS n,stars AS s,rub AS r,title,description AS d FROM positions WHERE in_storefront=1 ORDER BY sort_order,quantity')
        items=[dict(row) for row in await cur.fetchall()]
        return web.json_response({'products':items}, headers={'Cache-Control':'no-store'})
    except (AttributeError, RuntimeError):
        raise web.HTTPServiceUnavailable(text='Каталог временно недоступен')

async def health(request):
    task=request.app.get('bot_task')
    ok=request.app['preview'] or (task is not None and not task.done())
    return web.json_response({'ok':ok,'mode':'preview' if request.app['preview'] else 'telegram'},status=200 if ok else 503)

async def bot_lifecycle(app):
    if app['preview']:
        from pathlib import Path
        import aiosqlite
        path=os.getenv('PREVIEW_DB_PATH','/tmp/medicine-storefront-preview.db')
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        bot.db=await aiosqlite.connect(path); bot.db.row_factory=aiosqlite.Row
        await bot.init_db()
    else:
        task=asyncio.create_task(bot.main())
        app['bot_task']=task
        def finished(t):
            if not t.cancelled() and t.exception():
                logging.error('Telegram worker stopped; check runtime configuration')
        task.add_done_callback(finished)
    yield
    task=app.get('bot_task')
    if task:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    if app['preview']: await bot.db.close()

def create_app():
    if not PREVIEW and not bot.BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN is required. Use PREVIEW_MODE=1 only for sandbox preview.')
    app=web.Application()
    app['preview']=PREVIEW
    app.cleanup_ctx.append(bot_lifecycle)
    app.router.add_get('/',index)
    app.router.add_get('/health',health)
    app.router.add_get('/api/catalog',catalog)
    from pathlib import Path
    app.router.add_static('/assets/', Path(__file__).with_name('assets'), show_index=False)
    return app

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(),host='0.0.0.0',port=3000,access_log=None)
