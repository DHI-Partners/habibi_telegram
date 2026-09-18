"""Вебхук регистрируется только на адрес, по которому сайт реально отвечает.

Telegram принимает любой https-адрес и молча шлёт в него апдейты. Устаревший
host_name в site_config.json (так было после переезда erp на новый домен) или
имя сайта, которого нет в DNS, давали бота, которому никто не может написать,
— и ни одной ошибки на форме.
"""

from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

BOT = "webhook-url-test-bot"


def make_bot():
	if frappe.db.exists("Telegram Bot", BOT):
		return frappe.get_doc("Telegram Bot", BOT)
	with patch(
		"habibi_telegram.telegram_api.TelegramBotAPI.get_me",
		return_value={"is_bot": True, "username": "webhook_url_test_bot"},
	):
		return frappe.get_doc({"doctype": "Telegram Bot", "title": BOT, "api_token": "1:test"}).insert()


def response(status, body):
	return Mock(status_code=status, text=body, json=Mock(side_effect=lambda: frappe.parse_json(body)))


class TestДоступностьАдреса(IntegrationTestCase):
	def setUp(self):
		self.bot = make_bot()
		self.api = Mock()

	def _set_webhook(self, get):
		with (
			patch("habibi_telegram.habibi_telegram.doctype.telegram_bot.telegram_bot.requests.get", get),
			patch.object(type(self.bot), "get_webhook_url", return_value="https://shop.example.org/hook"),
			patch.object(type(self.bot), "get_api", return_value=self.api),
			patch("frappe.msgprint"),
		):
			self.bot.set_webhook()

	def test_живой_сайт_регистрируется(self):
		get = Mock(return_value=response(200, '{"message": "pong"}'))
		self._set_webhook(get)
		get.assert_called_once()
		self.assertEqual(get.call_args.args[0], "https://shop.example.org/api/method/ping")
		self.api.set_webhook.assert_called_once()

	def test_чужой_ответ_не_регистрируется(self):
		# 404 от прокси: домен смотрит на сервер, но сайта там нет
		with self.assertRaises(frappe.ValidationError) as cm:
			self._set_webhook(Mock(return_value=response(404, "404 page not found")))
		self.assertIn("https://shop.example.org", str(cm.exception))
		self.api.set_webhook.assert_not_called()

	def test_не_frappe_не_регистрируется(self):
		# 200, но не наш ping — например, заглушка хостинга на старом домене
		with self.assertRaises(frappe.ValidationError):
			self._set_webhook(Mock(return_value=response(200, "<html>parked</html>")))
		self.api.set_webhook.assert_not_called()

	def test_недоступный_домен_не_регистрируется(self):
		get = Mock(side_effect=requests.ConnectionError("Name or service not known"))
		with self.assertRaises(frappe.ValidationError):
			self._set_webhook(get)
		self.api.set_webhook.assert_not_called()
