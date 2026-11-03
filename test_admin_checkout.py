"""Regression: the earlier admin router must not swallow checkout payloads."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import aiosqlite
from aiogram import Bot
from aiogram.types import Message, Chat, User, MessageEntity
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.dispatcher.event.bases import UNHANDLED
import bot as b
import admin

class AdminCheckoutRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        b.db = await aiosqlite.connect(':memory:')
        b.db.row_factory = aiosqlite.Row
        await b.init_db()
        self.storage = MemoryStorage()
        self.uid = next(iter(b.ADMIN_IDS))
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=self.uid, user_id=self.uid))
        self.bot = Bot('123456:TEST_TOKEN')

    async def asyncTearDown(self):
        await self.bot.session.close()
        await self.storage.close()
        await b.db.close()

    async def dispatch(self, text):
        message = Message(message_id=1, date=datetime.now(timezone.utc),
                          chat=Chat(id=self.uid, type='private'),
                          from_user=User(id=self.uid, is_bot=False, first_name='Admin'),
                          text=text, entities=[MessageEntity(type='bot_command', offset=0, length=len(text.split()[0]))])
        with patch.object(Message, 'answer', new_callable=AsyncMock) as answer:
            result = await b.admin_router.propagate_event('message', message, state=self.state, bot=self.bot)
            self.assertIsNot(result, UNHANDLED)
            return answer

    async def test_admin_checkout_payload_survives_router(self):
        await self.state.update_data(old='stale admin state')
        await self.state.set_state(b.Admin.edit_text)
        answer = await self.dispatch('/start order_60')
        self.assertEqual(await self.state.get_state(), b.Order.link.state)
        self.assertEqual(await self.state.get_data(), {'quantity': 60})
        self.assertIn(await b.get_text('ask_link'), answer.call_args.args[0])
        self.assertEqual(await b.num('SELECT COUNT(*) FROM orders'), 0)

    async def test_plain_admin_start_still_shows_menu(self):
        await self.dispatch('/start order_100')
        answer = await self.dispatch('/start')
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.assertIn('reply_markup', answer.call_args.kwargs)

    async def test_admin_command_still_opens_panel(self):
        answer = await self.dispatch('/admin')
        self.assertIn('Админ-панель', answer.call_args.args[0])

    async def test_admin_unavailable_package_is_rejected(self):
        await self.dispatch('/start order_999')
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

if __name__ == '__main__':
    unittest.main()
