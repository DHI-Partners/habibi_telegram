"""Ответить можно и человеку, которого аккаунт увидел только что.

MTProto требует access_hash, а он берётся из кэша сущностей в сессии. Новый
клиент написал слушателю, в сессию отправителя он ещё не попал — раньше ответ
ИИ не уходил («аккаунт ещё не знает чат»), и клиент оставался без ответа.
"""

import asyncio

import frappe
from frappe.tests import IntegrationTestCase

from habibi_telegram import user_client


class FakeClient:
	"""Клиент Telethon: сущность известна только после перечитывания диалогов."""

	def __init__(self, known_after_dialogs=True):
		self.known = False
		self.known_after_dialogs = known_after_dialogs
		self.dialog_calls = 0

	async def get_entity(self, chat_id):
		if not self.known:
			raise ValueError(f"Could not find the input entity for {chat_id}")
		return f"entity:{chat_id}"

	async def get_dialogs(self, limit=None):
		self.dialog_calls += 1
		self.known = self.known_after_dialogs
		return []


class TestResolvePeer(IntegrationTestCase):
	def test_незнакомого_собеседника_находим_через_список_диалогов(self):
		client = FakeClient()
		entity = asyncio.run(user_client._resolve(client, "8205286129"))
		self.assertEqual(entity, "entity:8205286129")
		self.assertEqual(client.dialog_calls, 1)

	def test_известного_не_гоняем_за_диалогами(self):
		client = FakeClient()
		client.known = True
		asyncio.run(user_client._resolve(client, "8205286129"))
		self.assertEqual(client.dialog_calls, 0)

	def test_не_нашли_и_после_диалогов_понятная_ошибка(self):
		client = FakeClient(known_after_dialogs=False)
		with self.assertRaises(frappe.ValidationError) as ctx:
			asyncio.run(user_client._resolve(client, "8205286129"))
		self.assertIn("8205286129", str(ctx.exception))
		self.assertEqual(client.dialog_calls, 1)
