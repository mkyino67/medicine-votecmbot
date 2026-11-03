import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import aiosqlite
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
import bot as b
import admin
import storefront_admin as sf
import server

class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        b.db=await aiosqlite.connect(':memory:'); b.db.row_factory=aiosqlite.Row
        await b.init_db()
        self.storage=MemoryStorage()
        self.state=FSMContext(self.storage,StorageKey(bot_id=1,chat_id=42,user_id=42))
        self.message=SimpleNamespace(text='', answer=AsyncMock())
    async def asyncTearDown(self):
        await self.storage.close(); await b.db.close()
    async def save(self, field, text, qty=None):
        await self.state.set_state(sf.Edit.value); await self.state.update_data(field=field,qty=qty)
        self.message.text=text; await sf.save(self.message,self.state)
    async def test_initial_and_idempotent(self):
        self.assertEqual(await b.num('SELECT COUNT(*) FROM positions WHERE in_storefront=1'),6)
        await b.db.execute('UPDATE positions SET title=?,in_storefront=0,sort_order=2 WHERE quantity=60',('New',)); await b.db.commit()
        await b.init_db()
        self.assertEqual(await b.num('SELECT COUNT(*) FROM positions WHERE quantity=60 AND title="New" AND in_storefront=0 AND sort_order=2'),1)
    async def test_add_and_shared_price(self):
        await self.save('new','900 300 400')
        self.assertEqual(await b.get_position(900),(300,400))
        self.assertEqual(await b.num('SELECT in_storefront FROM positions WHERE quantity=900'),1)
        await self.save('prices','310 420',900)
        self.assertEqual(await b.get_position(900),(310,420))
    async def test_duplicate_and_invalid(self):
        for text in ['100 1 1','0 1 1','900 -1 2','900 1','1000001 1 1']:
            await self.save('new',text)
            self.assertEqual(await self.state.get_state(),sf.Edit.value.state)
        self.assertEqual(await b.get_position(100),(30,50))
    async def test_metadata(self):
        await self.save('title','<Test>',100)
        await self.save('description','Description',100)
        await self.save('sort_order','1',100)
        text,_=await sf.card(100)
        self.assertIn('&lt;Test&gt;',text)
        self.assertIn('Description',text)
    async def test_api_hidden_and_sorted(self):
        import json
        await b.db.execute('UPDATE positions SET in_storefront=0 WHERE quantity=60')
        await b.db.execute('UPDATE positions SET sort_order=0 WHERE quantity=200')
        result=json.loads((await server.catalog(None)).text)['products']
        self.assertEqual(result[0]['n'],200)
        self.assertNotIn(60,[p['n'] for p in result])
    async def test_delete_confirmation_and_pagination(self):
        cb=SimpleNamespace(data='sf:delete:100',message=SimpleNamespace(edit_text=AsyncMock()),answer=AsyncMock())
        await sf.manage(cb,self.state)
        self.assertIsNotNone(await b.get_position(100))
        cb.data='sf:remove:100'; await sf.manage(cb,self.state)
        self.assertIsNone(await b.get_position(100))
        for page in [0,1,2,100]:
            text,kb=await sf.catalog(page)
            self.assertLess(len(kb.inline_keyboard),12)
    async def test_legacy_add_publishes(self):
        await self.state.set_state(b.Admin.add_price); await self.state.update_data(qty=999)
        self.message.text='100 200'; await admin.set_price(self.message,self.state)
        self.assertEqual(await b.num('SELECT in_storefront FROM positions WHERE quantity=999'),1)
