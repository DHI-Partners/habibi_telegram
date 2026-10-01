"""Превью чата обновляется при каждом сообщении — но сообщение важнее превью.

На проде MariaDB отвечает 1020 «Record has changed since last read», когда
строку Telegram Chat успел изменить параллельный процесс (слушатель, ответ ИИ),
пока наша транзакция держит старый снимок. Раньше это роняло вставку
сообщения: ответ клиенту уже ушёл, а в журнале его нет, и кабинет считал, что
отправка не удалась, — оператор нажимал снова, и клиент получал дубли.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

CHAT_ID = "5559001"


def _chat():
	return frappe.get_doc({"doctype": "Telegram Chat", "chat_id": CHAT_ID, "type": "private", "title": "Превью"}).insert()


def _message(chat, text="привет"):
	return frappe.get_doc(
		{"doctype": "Telegram Message", "chat": chat.name, "message_id": 1, "direction": "Incoming", "content": text}
	)


def _deferred_preview(callbacks):
	"""Среди колбэков after_commit есть и чужие (realtime-событие сообщения) — берём только отложенное превью."""
	return [c for c in callbacks if getattr(c, "__name__", "") == "_write_chat_preview_after_commit"]


def _failing_chat_update(original):
	"""set_value, который на строке чата падает как в бою, а на остальном работает как обычно."""

	def wrapper(doctype, *args, **kwargs):
		if doctype == "Telegram Chat":
			raise frappe.QueryDeadlockError("(1020, \"Record has changed since last read in table 'tabTelegram Chat'\")")
		return original(doctype, *args, **kwargs)

	return wrapper


class TestChatPreview(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_обычная_вставка_обновляет_превью(self):
		chat = _chat()
		_message(chat, "здравствуйте").insert()
		self.assertEqual(frappe.db.get_value("Telegram Chat", chat.name, "last_message_content"), "здравствуйте")

	def test_сбой_превью_не_роняет_запись_сообщения(self):
		chat = _chat()
		message = _message(chat)
		with patch.object(frappe.db, "set_value", side_effect=_failing_chat_update(frappe.db.set_value)):
			message.insert()  # раньше падало QueryDeadlockError, и сообщение терялось
		self.assertTrue(frappe.db.exists("Telegram Message", message.name))

	def test_превью_дописывается_после_коммита(self):
		chat = _chat()
		message = _message(chat, "дописать после")
		callbacks = []
		with (
			patch.object(frappe.db, "set_value", side_effect=_failing_chat_update(frappe.db.set_value)),
			patch.object(frappe.db, "after_commit", SimpleNamespace(add=callbacks.append)),
		):
			message.insert()
		(deferred,) = _deferred_preview(callbacks)
		# Коммит прошёл, строка чата больше никому не мешает — отложенная запись проходит
		with patch.object(frappe.db, "commit"):
			deferred()
		self.assertEqual(frappe.db.get_value("Telegram Chat", chat.name, "last_message_content"), "дописать после")

	def test_сбой_отложенной_записи_не_ломает_коммит(self):
		chat = _chat()
		message = _message(chat)
		callbacks = []
		with (
			patch.object(frappe.db, "set_value", side_effect=_failing_chat_update(frappe.db.set_value)),
			patch.object(frappe.db, "after_commit", SimpleNamespace(add=callbacks.append)),
		):
			message.insert()
			(deferred,) = _deferred_preview(callbacks)
			with patch("frappe.log_error") as log_error:
				deferred()  # set_value по-прежнему падает — но исключение наружу не идёт
		log_error.assert_called_once()
