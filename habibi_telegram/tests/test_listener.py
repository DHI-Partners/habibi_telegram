"""Слушатель и cron не должны работать с одной сессией одновременно.

Одна сессия Telethon в двух соединениях — повод для Telegram разорвать её
(AUTH_KEY_DUPLICATED). Поэтому cron пропускает аккаунт, пока жив heartbeat
слушателя, и возвращается к нему сам, когда heartbeat протух.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from habibi_telegram import listener, user_client

TITLE = "listener-test-account"


class TestHeartbeat(IntegrationTestCase):
	def setUp(self):
		if not frappe.db.exists("Telegram Account", TITLE):
			frappe.get_doc({
				"doctype": "Telegram Account", "title": TITLE, "phone": "+70000000001",
				"api_id": "1", "api_hash": "x",
			}).insert()
		frappe.db.set_value("Telegram Account", TITLE, {"enabled": 1, "sync_enabled": 1, "status": "Connected"})
		# should_listen делает rollback, чтобы видеть свежие данные, — без
		# коммита он откатил бы и эту подготовку
		frappe.db.commit()
		frappe.cache().delete_value(listener._heartbeat_key(TITLE))

	@classmethod
	def tearDownClass(cls):
		# Фиктивный Connected-аккаунт не должен оставаться в dev-базе — иначе
		# его подхватит настоящий bench telegram listen-all
		frappe.db.delete("Telegram Account", TITLE)
		frappe.db.commit()
		super().tearDownClass()

	def test_без_слушателя_cron_синхронизирует(self):
		with patch("frappe.enqueue") as enqueue:
			user_client.sync_all_accounts()
		accounts = [c.kwargs["account"] for c in enqueue.call_args_list]
		self.assertIn(TITLE, accounts)

	def test_живой_слушатель_отключает_cron(self):
		listener.mark_alive(TITLE)
		self.assertTrue(listener.is_alive(TITLE))
		with patch("frappe.enqueue") as enqueue:
			user_client.sync_all_accounts()
		accounts = [c.kwargs["account"] for c in enqueue.call_args_list]
		self.assertNotIn(TITLE, accounts)

	def test_слушать_только_подключённые(self):
		self.assertTrue(listener.should_listen(TITLE))
		frappe.db.set_value("Telegram Account", TITLE, "sync_enabled", 0)
		frappe.db.commit()
		self.assertFalse(listener.should_listen(TITLE))
