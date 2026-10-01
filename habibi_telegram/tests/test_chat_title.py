"""Название личного чата — имя человека из Telegram, а не его алиас.

Алиас (@username) остаётся запасным вариантом, когда имя скрыто или стёрто:
человеку в списке переписок нужно имя, по которому он сам себя назвал.
"""

import frappe
from frappe.tests import IntegrationTestCase

from habibi_telegram.habibi_telegram.doctype.telegram_chat.telegram_chat import get_or_create

CHAT_ID = 5559101


def _private(**fields):
	return {"id": CHAT_ID, "type": "private", **fields}


class TestChatTitle(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _title(self):
		return frappe.db.get_value("Telegram Chat", {"chat_id": str(CHAT_ID)}, "title")

	def test_личный_чат_называется_именем_а_не_алиасом(self):
		get_or_create(_private(username="dontRepeatYourself", first_name="FSA", last_name="Kirsanov"))
		self.assertEqual(self._title(), "FSA Kirsanov")

	def test_только_имя_без_фамилии(self):
		get_or_create(_private(username="dontRepeatYourself", first_name="FSA"))
		self.assertEqual(self._title(), "FSA")

	def test_без_имени_запасной_вариант_алиас(self):
		get_or_create(_private(username="dontRepeatYourself"))
		self.assertEqual(self._title(), "dontRepeatYourself")

	def test_без_имени_и_алиаса_номер_чата(self):
		get_or_create(_private())
		self.assertEqual(self._title(), str(CHAT_ID))

	def test_прежнее_название_алиас_исправляется_именем(self):
		get_or_create(_private(username="dontRepeatYourself"))
		get_or_create(_private(username="dontRepeatYourself", first_name="FSA"))
		self.assertEqual(self._title(), "FSA")

	def test_название_группы_не_меняется(self):
		get_or_create({"id": CHAT_ID, "type": "supergroup", "title": "Кухня", "username": "kitchen_grp"})
		self.assertEqual(self._title(), "Кухня")
