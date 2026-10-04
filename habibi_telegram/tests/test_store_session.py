"""Сохранение сессии не должно ломать уже сделанное.

Слушатель и отправитель пишут одну строку Telegram Account. На проде MariaDB
отвечает 1020 «Record has changed since last read», и раньше это исключение
вылетало из `finally` после отправки: сообщение клиенту ушло, а система считала
отправку неудачной.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from habibi_telegram import mtproto


class FakeSession:
	def save(self):
		return "строка-сессии"

	def dump_cache(self):
		return [[1, 2]]


class FakeAccount:
	doctype = "Telegram Account"
	name = "Тестовый аккаунт"

	def is_new(self):
		return False


def _conflict(*args, **kwargs):
	raise frappe.QueryDeadlockError("(1020, \"Record has changed since last read in table 'tabTelegram Account'\")")


class TestStoreSession(IntegrationTestCase):
	def test_разовый_конфликт_повторяется_и_проходит(self):
		calls = []

		def flaky(*args, **kwargs):
			calls.append(1)
			if len(calls) == 1:
				_conflict()

		with (
			patch.object(mtproto, "set_encrypted_password"),
			patch.object(frappe.db, "set_value", side_effect=flaky),
			patch.object(frappe.db, "rollback"),
		):
			mtproto.store_session(FakeAccount(), FakeSession())
		self.assertEqual(len(calls), 2)

	def test_постоянный_конфликт_не_роняет_вызывающего(self):
		with (
			patch.object(mtproto, "set_encrypted_password"),
			patch.object(frappe.db, "set_value", side_effect=_conflict),
			patch.object(frappe.db, "rollback"),
			patch("frappe.log_error") as log_error,
		):
			mtproto.store_session(FakeAccount(), FakeSession())  # раньше падало 1020
		log_error.assert_called_once()
