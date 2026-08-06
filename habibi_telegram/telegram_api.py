"""
Тонкая обёртка над Telegram Bot API.

Bot API — это обычный HTTPS с JSON, поэтому никаких внешних библиотек не нужно:
`requests` приходит вместе с frappe. Это осознанная замена python-telegram-bot,
который намертво прибит к Python <= 3.12 и на Frappe v16 (Python 3.14) не заводится.

Документация: https://core.telegram.org/bots/api
"""

import json
import mimetypes
import os

import frappe
import requests
from frappe import _

API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = 30

# Ограничения Telegram, https://core.telegram.org/bots/api#sendmessage
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024


class TelegramAPIError(frappe.ValidationError):
	"""Telegram ответил ok=false либо запрос не дошёл."""

	def __init__(self, message, error_code=None, description=None):
		super().__init__(message)
		self.error_code = error_code
		self.description = description


class ParseMode:
	"""https://core.telegram.org/bots/api#formatting-options"""

	HTML = "HTML"
	MARKDOWN_V2 = "MarkdownV2"
	MARKDOWN = "Markdown"

	@classmethod
	def values(cls):
		return [cls.HTML, cls.MARKDOWN_V2, cls.MARKDOWN]


class TelegramBotAPI:
	"""
	Клиент одного бота. Создаётся из документа Telegram Bot — см. `get_bot()`
	в habibi_telegram.client.
	"""

	def __init__(self, token: str, bot_name: str = None, timeout: int = DEFAULT_TIMEOUT):
		if not token:
			frappe.throw(_("Telegram Bot API token is empty"))

		self.token = token
		self.bot_name = bot_name
		self.timeout = timeout

	# -- транспорт ----------------------------------------------------------

	def call(self, method: str, payload: dict = None, files: dict = None):
		"""
		Вызвать метод Bot API. Возвращает содержимое поля `result`.

		При files=None запрос уходит как JSON, иначе — как multipart, и тогда
		вложенные значения (reply_markup) приходится сериализовать вручную.
		"""
		url = f"{API_BASE}/bot{self.token}/{method}"
		payload = {k: v for k, v in (payload or {}).items() if v is not None}

		try:
			if files:
				response = requests.post(
					url, data=_flatten(payload), files=files, timeout=self.timeout
				)
			else:
				response = requests.post(url, json=payload, timeout=self.timeout)
		except requests.RequestException as e:
			raise TelegramAPIError(
				_("Could not reach Telegram API: {0}").format(str(e))
			) from e

		try:
			data = response.json()
		except ValueError:
			raise TelegramAPIError(
				_("Telegram API returned a non-JSON response ({0})").format(response.status_code)
			)

		if not data.get("ok"):
			description = data.get("description") or response.text
			raise TelegramAPIError(
				_("Telegram API error {0}: {1}").format(data.get("error_code"), description),
				error_code=data.get("error_code"),
				description=description,
			)

		return data.get("result")

	# -- методы -------------------------------------------------------------

	def get_me(self):
		return self.call("getMe")

	def send_message(
		self,
		chat_id,
		text: str,
		parse_mode: str = None,
		reply_markup=None,
		reply_to_message_id=None,
		disable_web_page_preview=None,
		disable_notification=None,
	):
		"""text: 1–4096 символов. Пустую строку Telegram не принимает."""
		return self.call(
			"sendMessage",
			{
				"chat_id": chat_id,
				"text": text,
				"parse_mode": parse_mode,
				"reply_markup": reply_markup,
				"reply_to_message_id": reply_to_message_id,
				"disable_web_page_preview": disable_web_page_preview,
				"disable_notification": disable_notification,
			},
		)

	def send_document(
		self,
		chat_id,
		document,
		filename: str = None,
		caption: str = None,
		parse_mode: str = None,
	):
		"""
		document может быть:
		  - str  — file_id, уже известный Telegram, либо публичный URL
		  - bytes / file-like — содержимое файла, уйдёт multipart'ом
		"""
		payload = {
			"chat_id": chat_id,
			"caption": caption,
			"parse_mode": parse_mode,
		}

		if isinstance(document, str):
			payload["document"] = document
			return self.call("sendDocument", payload)

		content = document.read() if hasattr(document, "read") else document
		name = filename or getattr(document, "name", None) or "file"
		name = os.path.basename(name)
		mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

		return self.call("sendDocument", payload, files={"document": (name, content, mime)})

	def send_voice(
		self,
		chat_id,
		voice,
		filename: str = "voice.ogg",
		mime: str = "audio/ogg",
		caption: str = None,
		parse_mode: str = None,
		duration: int = None,
	):
		"""
		Голосовое сообщение — то самое, с волной и кружком, а не вложение.

		Telegram признаёт голосовым только OGG/OPUS, MP3 и M4A; что угодно
		другое приедет в чат обычным аудиофайлом. Приведение — на стороне
		habibi_telegram.utils.audio.
		"""
		payload = {
			"chat_id": chat_id,
			"caption": caption,
			"parse_mode": parse_mode,
			"duration": duration,
		}

		if isinstance(voice, str):
			payload["voice"] = voice
			return self.call("sendVoice", payload)

		content = voice.read() if hasattr(voice, "read") else voice

		return self.call(
			"sendVoice", payload, files={"voice": (os.path.basename(filename), content, mime)}
		)

	def get_file(self, file_id: str):
		"""Где лежит файл на серверах Telegram. Ссылка живёт около часа."""
		return self.call("getFile", {"file_id": file_id})

	def download_file(self, file_id: str, max_bytes: int = None) -> tuple[bytes, str]:
		"""
		Скачать вложение по его file_id. Возвращает (содержимое, имя файла).

		Ботам Telegram отдаёт только файлы до 20 МБ — всё, что больше, придётся
		смотреть в самом Telegram.
		"""
		info = self.get_file(file_id) or {}
		path = info.get("file_path")

		if not path:
			raise TelegramAPIError(_("Telegram did not tell where the file is"))

		size = info.get("file_size") or 0
		if max_bytes and size > max_bytes:
			raise TelegramAPIError(
				_("Attachment is too large: {0} MB").format(round(size / 1024 / 1024, 1))
			)

		url = f"{API_BASE}/file/bot{self.token}/{path}"

		try:
			response = requests.get(url, timeout=self.timeout)
			response.raise_for_status()
		except requests.RequestException as e:
			raise TelegramAPIError(
				_("Could not download the attachment: {0}").format(str(e))
			) from e

		return response.content, os.path.basename(path)

	def edit_message_text(
		self,
		chat_id,
		message_id,
		text: str,
		parse_mode: str = None,
		reply_markup=None,
		disable_web_page_preview=None,
	):
		"""
		Править бот может только свои сообщения и только 48 часов —
		дальше Telegram отвечает "message can't be edited".
		"""
		return self.call(
			"editMessageText",
			{
				"chat_id": chat_id,
				"message_id": message_id,
				"text": text,
				"parse_mode": parse_mode,
				"reply_markup": reply_markup,
				"disable_web_page_preview": disable_web_page_preview,
			},
		)

	def delete_message(self, chat_id, message_id):
		return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

	def answer_callback_query(self, callback_query_id, text: str = None, show_alert: bool = False):
		return self.call(
			"answerCallbackQuery",
			{
				"callback_query_id": callback_query_id,
				"text": text,
				"show_alert": show_alert or None,
			},
		)

	# -- webhook ------------------------------------------------------------

	def set_webhook(
		self,
		url: str,
		secret_token: str = None,
		drop_pending_updates: bool = False,
		allowed_updates: list = None,
		max_connections: int = None,
	):
		return self.call(
			"setWebhook",
			{
				"url": url,
				"secret_token": secret_token,
				"drop_pending_updates": drop_pending_updates or None,
				"allowed_updates": allowed_updates,
				"max_connections": max_connections,
			},
		)

	def delete_webhook(self, drop_pending_updates: bool = False):
		return self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates or None})

	def get_webhook_info(self):
		return self.call("getWebhookInfo")


def _flatten(payload: dict) -> dict:
	"""multipart не умеет вложенные структуры — упаковываем их в JSON-строки."""
	out = {}
	for key, value in payload.items():
		out[key] = json.dumps(value) if isinstance(value, dict | list) else value
	return out
