"""Пакетное удаление сообщений через личный аккаунт: пачки по 100 и «у всех»."""

from unittest.mock import AsyncMock, MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from habibi_telegram import user_client


class TestDeleteMessages(IntegrationTestCase):
	def _run(self, ids, revoke=True):
		client = MagicMock()
		client.delete_messages = AsyncMock(return_value=None)

		def call(account, op):
			import asyncio

			return asyncio.run(op(client))

		with (
			patch.object(user_client, "get_account", return_value="acc"),
			patch.object(user_client.mtproto, "require_telethon"),
			patch.object(user_client.mtproto, "call", side_effect=call),
			patch.object(user_client, "_resolve", AsyncMock(return_value="entity")),
			patch.object(user_client.store, "mark_deleted", return_value=len(ids)) as marked,
		):
			result = user_client.delete_messages("acc", "123", ids, revoke=revoke)
		return result, client.delete_messages, marked

	def test_ровно_одна_пачка_если_сообщений_немного(self):
		result, delete, marked = self._run([1, 2, 3])
		self.assertEqual(result, 3)
		delete.assert_awaited_once_with("entity", [1, 2, 3], revoke=True)
		marked.assert_called_once_with("acc", [1, 2, 3], chat_id="123")

	def test_пачки_по_сто(self):
		ids = list(range(1, 251))
		_result, delete, _ = self._run(ids)
		self.assertEqual([len(c.args[1]) for c in delete.await_args_list], [100, 100, 50])

	def test_revoke_можно_выключить(self):
		_r, delete, _ = self._run([5], revoke=False)
		delete.assert_awaited_once_with("entity", [5], revoke=False)

	def test_пустой_список_ничего_не_делает(self):
		with patch.object(user_client.mtproto, "call") as call:
			self.assertEqual(user_client.delete_messages("acc", "1", []), 0)
		call.assert_not_called()

	def test_ошибка_telegram_не_помечает_удалённым(self):
		with (
			patch.object(user_client, "get_account", return_value="acc"),
			patch.object(user_client.mtproto, "require_telethon"),
			patch.object(user_client.mtproto, "call", side_effect=RuntimeError("flood")),
			patch.object(user_client.mtproto, "describe_error", return_value="Telegram не принял"),
			patch.object(user_client.store, "mark_deleted") as marked,
		):
			with self.assertRaises(frappe.ValidationError):
				user_client.delete_messages("acc", "1", [1, 2])
		marked.assert_not_called()


class TestClearHistory(IntegrationTestCase):
	"""«Очистить чат в Telegram» идёт по тому, что реально лежит в Telegram, а не по нашей базе:
	после чистки базы старые сообщения известны только самому Telegram."""

	def _client(self, history, left):
		"""history — id сообщений в чате до удаления; left — сколько останется после (Telegram мог не дать стереть всё)."""
		state = {"phase": "before"}

		async def iter_messages(entity, limit=None):
			ids = history if state["phase"] == "before" else history[:left]
			for i in ids[:limit]:
				yield MagicMock(id=i)

		async def delete_messages(entity, ids, revoke=True):
			state["phase"] = "after"

		client = MagicMock()
		client.iter_messages = iter_messages
		client.delete_messages = AsyncMock(side_effect=delete_messages)
		return client

	def _run(self, history, left=0):
		client = self._client(history, left)

		def call(account, op):
			import asyncio

			return asyncio.run(op(client))

		with (
			patch.object(user_client, "get_account", return_value="acc"),
			patch.object(user_client.mtproto, "require_telethon"),
			patch.object(user_client.mtproto, "call", side_effect=call),
			patch.object(user_client, "_resolve", AsyncMock(return_value="entity")),
			patch.object(user_client.store, "mark_deleted", return_value=0) as marked,
		):
			result = user_client.clear_history("acc", "123")
		return result, client, marked

	def test_стирает_всё_что_есть_в_telegram_даже_если_наша_база_пуста(self):
		result, client, marked = self._run(list(range(1, 59)))
		self.assertEqual(result, {"found": 58, "left": 0})
		sent = [c.args[1] for c in client.delete_messages.await_args_list]
		self.assertEqual(sum(len(s) for s in sent), 58)
		self.assertTrue(all(c.kwargs["revoke"] for c in client.delete_messages.await_args_list))
		marked.assert_called_once()

	def test_пачки_по_сто(self):
		_r, client, _m = self._run(list(range(1, 251)))
		self.assertEqual([len(c.args[1]) for c in client.delete_messages.await_args_list], [100, 100, 50])

	def test_честно_говорит_сколько_telegram_не_дал_стереть(self):
		result, _c, _m = self._run(list(range(1, 11)), left=3)
		self.assertEqual(result, {"found": 10, "left": 3})

	def test_пустой_чат(self):
		result, client, _m = self._run([])
		self.assertEqual(result, {"found": 0, "left": 0})
		client.delete_messages.assert_not_awaited()

	def test_ошибка_telegram_пробрасывается_и_базу_не_трогаем(self):
		with (
			patch.object(user_client, "get_account", return_value="acc"),
			patch.object(user_client.mtproto, "require_telethon"),
			patch.object(user_client.mtproto, "call", side_effect=RuntimeError("flood")),
			patch.object(user_client.mtproto, "describe_error", return_value="Telegram не принял"),
			patch.object(user_client.store, "mark_deleted") as marked,
		):
			with self.assertRaises(frappe.ValidationError):
				user_client.clear_history("acc", "1")
		marked.assert_not_called()
