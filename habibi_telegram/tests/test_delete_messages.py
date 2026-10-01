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
