"""Откуда сообщение и кто его отправил.

Поля telegram_bot, is_automated и is_bot нужны тем, кто строит поверх истории
автоматику: без них не понять, каким ботом отвечать, и не отличить ответ
человека от ответа обработчика или уведомления.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from habibi_telegram import client
from habibi_telegram.dispatcher import Context
from habibi_telegram.handlers import account as account_store
from habibi_telegram.handlers.logging import log_outgoing_message, pre_process

BOT = "origin-test-bot"


def make_bot():
	if frappe.db.exists("Telegram Bot", BOT):
		return frappe.get_doc("Telegram Bot", BOT)
	with patch(
		"habibi_telegram.telegram_api.TelegramBotAPI.get_me",
		return_value={"is_bot": True, "username": "origin_test_bot"},
	):
		return frappe.get_doc({"doctype": "Telegram Bot", "title": BOT, "api_token": "1:test"}).insert()


def update(message_id, *, text="привет", sender_is_bot=False, chat_id=5550001):
	return {
		"update_id": message_id,
		"message": {
			"message_id": message_id,
			"date": int(datetime.now(UTC).timestamp()),
			"chat": {"id": chat_id, "type": "private", "first_name": "Анна"},
			"from": {"id": chat_id, "is_bot": sender_is_bot, "first_name": "Анна"},
			"text": text,
		},
	}


def sent(message_id, chat_id=5550001, text="ответ"):
	return {"message_id": message_id, "date": int(datetime.now(UTC).timestamp()),
		"chat": {"id": chat_id, "type": "private", "first_name": "Анна"}, "text": text}


class TestВходящиеБота(IntegrationTestCase):
	def setUp(self):
		self.bot = make_bot()

	def test_входящее_знает_своего_бота_и_время(self):
		context = Context(telegram_bot=self.bot, update=update(101))
		pre_process(context)
		message = context.telegram_message
		self.assertEqual(message.telegram_bot, BOT)
		self.assertIsNotNone(message.sent_on)
		self.assertEqual(message.is_automated, 0)

	def test_отправитель_бот_помечается(self):
		context = Context(telegram_bot=self.bot, update=update(102, sender_is_bot=True, chat_id=5550002))
		pre_process(context)
		self.assertEqual(frappe.db.get_value("Telegram User", context.telegram_user.name, "is_bot"), 1)


class TestИсходящиеБота(IntegrationTestCase):
	def setUp(self):
		make_bot()

	def tearDown(self):
		frappe.flags.in_telegram_update = False

	def test_ручная_отправка_не_автоматическая(self):
		doc = log_outgoing_message(BOT, sent(201))
		self.assertEqual(doc.is_automated, 0)
		self.assertEqual(doc.telegram_bot, BOT)

	def test_ответ_обработчика_автоматический(self):
		frappe.flags.in_telegram_update = True
		doc = log_outgoing_message(BOT, sent(202))
		self.assertEqual(doc.is_automated, 1)

	def test_automated_доезжает_через_send_message(self):
		api = Mock()
		api.send_message = Mock(return_value=sent(203, text="от ИИ"))
		with patch("habibi_telegram.client.get_bot", return_value=api):
			client.send_message("от ИИ", from_bot=BOT, chat_id=5550001, automated=True)
		name = frappe.db.get_value("Telegram Message", {"message_id": "203", "telegram_bot": BOT})
		self.assertEqual(frappe.db.get_value("Telegram Message", name, "is_automated"), 1)


class TestЛичныйАккаунт(IntegrationTestCase):
	def setUp(self):
		title = "origin-test-account"
		if frappe.db.exists("Telegram Account", title):
			self.account = frappe.get_doc("Telegram Account", title)
		else:
			self.account = frappe.get_doc({
				"doctype": "Telegram Account", "title": title, "phone": "+70000000000",
				"api_id": "1", "api_hash": "x", "account_id": "9990001",
			}).insert()

	def _message(self, message_id, out):
		from telethon.tl import types

		return SimpleNamespace(
			id=message_id, peer_id=types.PeerUser(user_id=7770001), from_id=None, out=out,
			message="текст", date=datetime.now(UTC), media=None, action=None,
		)

	def test_automated_ставится_по_параметру(self):
		name, _ = account_store.log_message(self.account, self._message(301, out=True), automated=True)
		self.assertEqual(frappe.db.get_value("Telegram Message", name, "is_automated"), 1)

	def test_исходящее_с_телефона_ручное(self):
		name, _ = account_store.log_message(self.account, self._message(302, out=True))
		self.assertEqual(frappe.db.get_value("Telegram Message", name, "is_automated"), 0)
