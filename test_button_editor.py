import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
import aiosqlite
import bot as b
import button_editor as editor
import handlers

class EditorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        b.db = await aiosqlite.connect(':memory:')
        b.db.row_factory = aiosqlite.Row
        await b.init_db()

    async def asyncTearDown(self):
        await b.db.close()
        editor._overrides.clear()

    async def test_label_style_and_callback_are_independent(self):
        await editor.save('adm:approve:rub', 'Оплата', text='Подтвердить рубли', style='success')
        button = handlers.review_kb(123).inline_keyboard[0][0]
        self.assertEqual(button.text, 'Подтвердить рубли')
        self.assertEqual(button.style, 'success')
        self.assertEqual(button.callback_data, 'adm:approve:123:rub')
        self.assertEqual(handlers.review_kb(456).inline_keyboard[0][0].text, button.text)
        self.assertNotEqual(handlers.review_kb(123).inline_keyboard[0][1].text, button.text)
        self.assertEqual(button.model_dump(exclude_none=True)['style'], 'success')

    async def test_reload_and_reset(self):
        await editor.save('confirm:yes', 'Да', text='Подтверждаю', style='primary')
        editor._overrides.clear()
        await editor.load()
        self.assertEqual(b.btn('Да','confirm:yes').text, 'Подтверждаю')
        await editor.reset('confirm:yes')
        await editor.load()
        self.assertEqual(b.btn('Да','confirm:yes').text, 'Да')
        self.assertIsNone(b.btn('Да','confirm:yes').style)
        self.assertEqual(await b.num('SELECT COUNT(*) FROM orders'), 0)

    async def test_validation_and_bad_storage(self):
        for text in ('', '   ', 'x'*65, 'a\nb', '\u200b'):
            with self.assertRaises(ValueError):
                await editor.save('confirm:yes','Да',text=text)
        with self.assertRaises(ValueError):
            await editor.save('confirm:yes','Да',style='purple')
        await b.set_setting('button:confirm:yes','not json')
        await editor.load()
        self.assertEqual(b.btn('Да','confirm:yes').text, 'Да')

    async def test_all_pages_and_details(self):
        for page in range(6):
            text, kb = await editor.listing(page)
            self.assertLess(len(text), 4096)
            for row in kb.inline_keyboard:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()),64)
                    if button.callback_data.startswith('be:open:'):
                        token = button.callback_data.split(':')[2]
                        key, default = await editor.resolve(token)
                        _, detail = await editor.detail(token, key, default)
                        self.assertEqual(detail.inline_keyboard[0][0].callback_data, 'be:sample')
        self.assertIsNone(await editor.resolve('9999'))

    async def test_position_tokens_stable_and_sample_safe(self):
        await b.db.execute('DELETE FROM positions WHERE quantity=60')
        self.assertEqual(await editor.resolve('q100'), ('qty:100','100'))
        self.assertIsNone(await editor.resolve('q60'))
        await editor.save('qty:100','100',text='Сто',style='danger')
        self.assertEqual((await b.qty_kb([(100,30,45)])).inline_keyboard[0][0].text,'Сто')
        cb = SimpleNamespace(data='be:sample', answer=AsyncMock())
        state = SimpleNamespace(clear=AsyncMock())
        await editor.editor_callback(cb,state)
        state.clear.assert_not_called()
        self.assertEqual(await b.num('SELECT COUNT(*) FROM orders'),0)

if __name__ == '__main__': unittest.main()
