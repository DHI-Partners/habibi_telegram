import frappe
from frappe import _
from frappe.model.document import Document


class TelegramMessage(Document):
	def after_insert(self):
		self.update_chat_preview()

	def update_chat_preview(self):
		frappe.db.set_value(
			"Telegram Chat",
			self.chat,
			{
				"last_message_on": self.sent_on or self.creation,
				"last_message_content": self.content,
			},
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

	# -- правка и удаление в самом Telegram ---------------------------------

	@frappe.whitelist()
	def edit_content(self, content: str, parse_mode: str = None):
		"""
		Переписать сообщение в чате.

		Править можно только свои сообщения и только 48 часов — дальше Telegram
		ответит ошибкой, и содержимое в базе останется прежним.
		"""
		self.check_permission("write")

		if not content:
			frappe.throw(_("Cannot save an empty message. Delete it instead."))

		if self.is_deleted:
			frappe.throw(_("This message has already been deleted in Telegram"))

		if self.direction != "Outgoing":
			frappe.throw(_("Only messages sent from here can be edited"))

		transport, sender = self.get_transport()

		if transport == "account":
			from habibi_telegram.user_client import edit_message

			# История правится там же: аккаунт получит собственный апдейт о правке
			edit_message(
				sender,
				chat_id=self.chat_id,
				message_id=self.message_id,
				text=content,
				parse_mode=parse_mode,
			)
		else:
			from habibi_telegram.client import get_bot

			get_bot(sender).edit_message_text(
				chat_id=self.chat_id,
				message_id=self.message_id,
				text=content,
				parse_mode=parse_mode,
			)
			self.db_set(
				{"content": content, "is_edited": 1, "edited_on": frappe.utils.now_datetime()},
				update_modified=False,
			)
			self.update_chat_preview()

		self.reload()

		return self.content

	@frappe.whitelist()
	def delete_in_telegram(self, revoke: int = 1):
		"""
		Удалить сообщение в чате. Запись в истории остаётся с пометкой.

		revoke=1 — удалить у всех участников, 0 — только у себя (умеет аккаунт;
		бот всегда удаляет у всех).
		"""
		self.check_permission("write")

		if self.is_deleted:
			return {"deleted": True}

		transport, sender = self.get_transport()

		if transport == "account":
			from habibi_telegram.user_client import delete_message

			delete_message(
				sender, chat_id=self.chat_id, message_id=self.message_id, revoke=bool(revoke)
			)
		else:
			from habibi_telegram.client import get_bot

			get_bot(sender).delete_message(chat_id=self.chat_id, message_id=self.message_id)
			self.db_set(
				{"is_deleted": 1, "deleted_on": frappe.utils.now_datetime()},
				update_modified=False,
			)

		self.reload()

		return {"deleted": True}

	# -- вспомогательное ----------------------------------------------------

	@property
	def chat_id(self):
		return frappe.db.get_value("Telegram Chat", self.chat, "chat_id")

	def get_transport(self) -> tuple[str, str]:
		"""
		Чьими руками работать с сообщением: личного аккаунта или бота.

		Сначала тот, через кого оно прошло, — у него точно есть доступ. Для
		входящих берём любого участника чата с нашей стороны: бот в группе с
		правами админа удалить чужое сообщение может, аккаунт — своё в любом
		диалоге.
		"""
		if self.telegram_account:
			return "account", self.telegram_account

		if self.from_bot:
			return "bot", self.from_bot

		chat = frappe.get_doc("Telegram Chat", self.chat)
		account = chat.get_account()

		if account:
			return "account", account

		if chat.bots:
			return "bot", chat.bots[0].telegram_bot

		frappe.throw(
			_("Neither a bot nor a personal account of this site takes part in chat {0}").format(
				chat.title
			)
		)
