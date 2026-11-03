import unittest
import aiosqlite
import bot as b
import handlers
import admin

class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        b.db=await aiosqlite.connect(':memory:')
        b.db.row_factory=aiosqlite.Row
        await b.init_db()
    async def asyncTearDown(self):
        await b.db.close()
    async def test_fresh_database(self):
        self.assertEqual(await b.num('SELECT COUNT(*) FROM orders'),0)
        self.assertEqual(len(await b.get_positions()),18)
        self.assertEqual(await b.get_position(100),(30,50))
        self.assertEqual(await b.get_dest('orders'),(-1003889093155,7))
        self.assertEqual(await b.get_dest('payments'),(-1004477091535,None))
    async def test_initialization_idempotent(self):
        await b.db.execute('DELETE FROM positions WHERE quantity=60'); await b.db.commit()
        await b.init_db()
        self.assertIsNone(await b.get_position(60))
        self.assertEqual(await b.num('PRAGMA user_version'),1)
    async def test_settings_and_texts(self):
        await b.set_setting('orders_open','0'); self.assertFalse(await b.orders_open())
        await b.set_setting('orders_chat','-123'); await b.set_setting('orders_thread','8')
        self.assertEqual(await b.get_dest('orders'),(-123,8))
        self.assertEqual(await b.get_text('start_text',min=60,max=800),'Выберите количество (от 60 до 800):')
        await b.set_setting('btn_max','0'); await b.set_setting('btn_custom','0')
        kb=await b.qty_kb([])
        self.assertEqual(kb.inline_keyboard[0][0].callback_data,'qty:custom')
    async def test_votes(self):
        await b.db.executemany('INSERT INTO votes VALUES (?,?,?)',[('p',1,0),('p',2,4),('p',3,5)])
        self.assertEqual(await b.poll_total('p'),120)
        await b.db.execute('INSERT OR REPLACE INTO votes VALUES (?,?,?)',('p',1,2))
        self.assertEqual(await b.poll_total('p'),160)
        await b.db.execute('DELETE FROM votes WHERE user_id=2')
        self.assertEqual(await b.poll_total('p'),60)
    async def test_all_admin_views(self):
        views=[await admin.home(),await admin.stats('today'),await admin.stats('7d'),await admin.stats('30d'),await admin.stats('all'),await admin.orders('all'),await admin.orders('pending'),await admin.card(1),await admin.positions(),await admin.position(100),await admin.menu(),await admin.texts(),await admin.destinations(),await admin.broadcast_view()]
        views += [await admin.text_view(k) for k in b.DEFAULT_TEXTS]
        for text,kb in views:
            self.assertTrue(text); self.assertLessEqual(len(text),4096)
            for row in kb.inline_keyboard:
                for button in row: self.assertLessEqual(len(button.callback_data.encode()),64)
    async def test_order_and_duplicates(self):
        cur=await b.db.execute("INSERT INTO orders (buyer_id,quantity,link,target_username,price_stars,price_rub,status) VALUES (1,100,'https://t.me/testchannel/12','@hello',30,45,'pending_approval')")
        await b.db.commit(); order=await b.get_order(cur.lastrowid)
        self.assertIn('Активных заказов на эту ссылку нет',await handlers.approval_text(order))
        self.assertEqual((await b.compute_stats('all'))['approval'],1)
        self.assertIn('Заказ #1',(await admin.card(1))[0])
    async def test_restored_presentation(self):
        self.assertTrue((await b.get_text('ask_link')).startswith('🔗'))
        self.assertTrue((await b.get_text('approved_text',order_id=1)).startswith('✅'))
        self.assertIn('⭐',await b.get_text('payment_text',order_id=1,rub=45,stars=30))
        kb=await b.qty_kb([])
        self.assertIn('✍️',[button.text[:2] for row in kb.inline_keyboard for button in row])
        text,kb=await admin.home()
        self.assertTrue(text.startswith('⚙️'))
        self.assertEqual(kb.inline_keyboard[0][0].text,'📊 Статистика')
        self.assertEqual(handlers.review_kb(1).inline_keyboard[0][1].text,'✅ Оплата в ⭐')
        await b.db.execute('INSERT INTO texts VALUES (?,?)',('ask_link','Мой текст'))
        self.assertEqual(await b.get_text('ask_link'),'Мой текст')

    def test_input_validation(self):
        self.assertTrue(b.LINK_RE.match('https://t.me/testchannel/12'))
        self.assertFalse(b.LINK_RE.match('https://example.com/12'))
        self.assertEqual(admin.price('30 45'),(30,45))
        self.assertIsNone(admin.price('0 45'))

if __name__=='__main__': unittest.main()
