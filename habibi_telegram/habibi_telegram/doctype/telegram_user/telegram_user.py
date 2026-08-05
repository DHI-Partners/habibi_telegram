import json

import frappe
from frappe.model.document import Document


class TelegramUser(Document):
	"""
	Телеграм-аккаунт, при желании связанный с пользователем Frappe.

	Здесь же живёт состояние текущего диалога. У python-telegram-bot оно лежало
	в памяти процесса (context.user_data), но на вебхуках каждый апдейт — это
	отдельный HTTP-запрос, и помнить между ними нечего. Поэтому состояние
	хранится в поле документа.
	"""

	def get_conversation_state(self) -> dict:
		if not self.conversation_state:
			return {}

		try:
			return json.loads(self.conversation_state)
		except (ValueError, TypeError):
			return {}

	def set_conversation_state(self, state: dict):
		self.db_set(
			"conversation_state",
			json.dumps(state, default=str) if state else "",
			update_modified=False,
		)

	def clear_conversation_state(self):
		self.set_conversation_state({})

	@property
	def frappe_user(self):
		return self.user or "Guest"


def get_or_create(telegram_user: dict) -> TelegramUser:
	"""
	Найти Telegram User по id из апдейта, при отсутствии — завести.

	telegram_user — объект User из Bot API:
	https://core.telegram.org/bots/api#user
	"""
	if not telegram_user or not telegram_user.get("id"):
		return None

	name = frappe.db.get_value("Telegram User", {"telegram_user_id": telegram_user["id"]})
	if name:
		return frappe.get_doc("Telegram User", name)

	full_name = " ".join(
		x for x in (telegram_user.get("first_name"), telegram_user.get("last_name")) if x
	)

	doc = frappe.get_doc(
		doctype="Telegram User",
		telegram_user_id=str(telegram_user["id"]),
		telegram_username=telegram_user.get("username"),
		full_name=full_name.strip() or str(telegram_user["id"]),
	)
	doc.insert(ignore_permissions=True)

	return doc
