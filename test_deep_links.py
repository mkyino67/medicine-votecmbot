"""Checkout links must select real packages without creating orders."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import aiosqlite
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
import bot as b
import handlers

class CheckoutLinksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        b.db = await aiosqlite.connect(':memory:')
        b.db.row_factory = aiosqlite.Row
        await b.init_db()
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=42, user_id=42))
        self.message = SimpleNamespace(chat=SimpleNamespace(type='private', id=42), from_user=SimpleNamespace(id=42), answer=AsyncMock())

    async def asyncTearDown(self):
        await self.storage.close()
        await b.db.close()

    async def run_start(self, payload=None):
        await handlers.start(self.message, self.state, CommandObject(command='start', args=payload))

    async def test_all_six_packages(self):
        for qty, stars, rub in [(60,15,25),(80,25,40),(100,30,50),(140,40,65),(160,50,75),(200,80,95)]:
            await self.state.update_data(link='old', target_username='old')
            await self.run_start(f'order_{qty}')
            self.assertEqual(await self.state.get_data(), {'quantity': qty})
            self.assertEqual(await self.state.get_state(), b.Order.link.state)
            text = self.message.answer.call_args.args[0]
            self.assertIn(f'{stars} звёзд / {rub} ₽', text)
            self.assertIn(await b.get_text('ask_link'), text)
        self.assertEqual(await b.num('SELECT COUNT(*) FROM orders'), 0)

    async def test_unavailable_and_malformed(self):
        for payload in ['order_999', 'order_', 'order_-60', 'order_60_extra', 'order_123456789', 'order_６０']:
            await self.run_start(payload)
            self.assertIsNone(await self.state.get_state())
            self.assertEqual(await self.state.get_data(), {})

    async def test_closed_shop(self):
        await b.set_setting('orders_open', '0')
        await self.run_start('order_100')
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.message.answer.call_args.args[0], await b.get_text('closed_text'))

    async def test_plain_start(self):
        await self.run_start('order_60')
        await self.run_start()
        self.assertIsNone(await self.state.get_state())
        self.assertIn('reply_markup', self.message.answer.call_args.kwargs)

    async def test_current_price_and_removed_package(self):
        await b.db.execute('UPDATE positions SET rub=55 WHERE quantity=100')
        await b.db.execute('DELETE FROM positions WHERE quantity=80')
        await b.db.commit()
        await b.init_db()
        self.assertEqual(await b.get_position(100), (30,55))
        self.assertIsNone(await b.get_position(80))
        await self.run_start('order_100')
        self.assertIn('55 ₽', self.message.answer.call_args.args[0])
        await self.run_start('order_80')
        self.assertIsNone(await self.state.get_state())

    async def test_group_does_not_start_checkout(self):
        self.message.chat.type = 'supergroup'
        await self.run_start('order_60')
        self.message.answer.assert_not_called()

    def test_storefront_links(self):
        page = Path('storefront.html').read_text()
        self.assertIn('https://t.me/irlmedicinbot?start=order_${p.n}', page)
        self.assertIn('Выбранный пакет передастся в бот', page)

if __name__ == '__main__':
    unittest.main()
