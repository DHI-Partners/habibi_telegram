import frappe
from frappe.model.document import Document


class TelegramMessage(Document):
	def after_insert(self):
		self.update_chat_preview()

	def update_chat_preview(self):
		frappe.db.set_value(
			"Telegram Chat",
			self.chat,
			{"last_message_on": self.creation, "last_message_content": self.content},
			update_modified=False,
		)

	def mark_as_password(self):
		"""
		Пользователь прислал пароль обычным сообщением — оно висит в истории
		чата и лежит у нас в базе. Звёздочки в базу, само сообщение из чата
		удаляем.
		"""
		masked = "*" * len(self.content or "")
		self.db_set("content", masked)
		self.update_chat_preview()

		chat = frappe.get_doc("Telegram Chat", self.chat)
		bot = chat.get_bot()
		if not bot:
			return

		try:
			bot.delete_message(chat_id=chat.chat_id, message_id=self.message_id)
		except Exception:
			# Удалять чужие сообщения можно только 48 часов и только с правами;
			# не смогли — не страшно, в базе уже звёздочки
			frappe.clear_last_message()
