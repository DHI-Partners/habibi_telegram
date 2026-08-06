"""
Личный телеграм-аккаунт, подключённый к сайту.

Бот видит только то, что написали лично ему. Аккаунт видит всё, что видит
человек: личные переписки, группы, каналы, уведомления сервисов и служебные
сообщения самого Telegram. Поэтому здесь другой протокол (MTProto вместо Bot
API), другой вход (номер телефона и код вместо токена) и другой способ получать
апдейты (см. habibi_telegram.mtproto).

Документ хранит ключ авторизации: он равносилен входу в аккаунт. Права на
DocType — только у System Manager и Telegram Bot Manager, ключ лежит в
хранилище паролей, а не в колонке таблицы.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document

from habibi_telegram import user_client


class TelegramAccount(Document):
	def validate(self):
		self.validate_phone()
		self.validate_api_id()

	def on_trash(self):
		"""Перед удалением — выйти из аккаунта и отцепиться от чатов."""
		if self.status == "Connected":
			try:
				user_client.log_out(self)
			except Exception:
				# Сессию могли отозвать раньше нас; удалению это не мешает
				frappe.clear_last_message()

		# Иначе ссылки из дочерних таблиц Telegram Chat не дадут удалить документ
		frappe.db.delete(
			"Telegram Account Item",
			{"telegram_account": self.name, "parenttype": "Telegram Chat"},
		)

	def validate_phone(self):
		if not self.phone:
			return

		phone = re.sub(r"[\s()\-]", "", self.phone)
		if not re.fullmatch(r"\+?\d{7,15}", phone):
			frappe.throw(_("Phone number should look like +79990000000"))

		self.phone = phone if phone.startswith("+") else "+" + phone

	def validate_api_id(self):
		if self.api_id and not str(self.api_id).strip().isdigit():
			frappe.throw(_("API ID is a number — take it from my.telegram.org"))

	# -- кнопки формы -------------------------------------------------------

	@frappe.whitelist()
	def request_code(self):
		"""Запросить код подтверждения на номер аккаунта."""
		self.check_permission("write")

		return user_client.request_code(self)

	@frappe.whitelist()
	def sign_in(self, code: str = None, password: str = None):
		"""
		Подтвердить вход.

		Код и пароль двухфакторки приходят параметрами и в базе не оседают.
		"""
		self.check_permission("write")

		return user_client.sign_in(self, code=code, password=password)

	@frappe.whitelist()
	def log_out(self):
		self.check_permission("write")

		return user_client.log_out(self)

	@frappe.whitelist()
	def sync_now(self):
		"""Забрать всё новое прямо сейчас, не дожидаясь планировщика."""
		self.check_permission("write")

		stats = user_client.sync_account(self)

		if stats.get("skipped"):
			frappe.msgprint(_("Sync is already running"))
		else:
			frappe.msgprint(
				_("New: {0}, edited: {1}, deleted: {2}").format(
					stats.get("new", 0), stats.get("edited", 0), stats.get("deleted", 0)
				),
				title=_("Synced"),
				indicator="green",
			)

		return stats

	@frappe.whitelist()
	def refresh_dialogs(self):
		"""Перечитать список диалогов — чаты появятся в разделе Telegram Chat."""
		self.check_permission("write")

		chats = user_client.fetch_dialogs(self)
		frappe.msgprint(_("Dialogs found: {0}").format(len(chats)))

		return chats

	@frappe.whitelist()
	def send_message(self, chat_id, message: str, parse_mode: str = None, reply_to=None):
		"""Написать в произвольный чат от имени аккаунта."""
		self.check_permission("write")

		return user_client.send_message(
			self, chat_id=chat_id, text=message, parse_mode=parse_mode, reply_to=reply_to
		)
