import frappe
from frappe.model.document import Document


class TelegramChat(Document):
	def get_bot(self):
		"""Первый бот, участвующий в чате. Нужен, чтобы ответить в этот же чат."""
		if not self.bots:
			return None

		from habibi_telegram.client import get_bot

		return get_bot(self.bots[0].telegram_bot)


def get_or_create(chat: dict, telegram_bot: str = None, telegram_user: str = None) -> TelegramChat:
	"""
	Найти чат по id из апдейта, при отсутствии — завести.

	Состав участников Bot API целиком не отдаёт, поэтому пополняем его по мере
	того, как от людей приходят сообщения.
	"""
	if not chat or not chat.get("id"):
		return None

	chat_id = str(chat["id"])
	title = (
		chat.get("title")
		or chat.get("username")
		or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
		or chat_id
	)

	name = frappe.db.get_value("Telegram Chat", {"chat_id": chat_id})

	if not name:
		doc = frappe.get_doc(
			doctype="Telegram Chat",
			chat_id=chat_id,
			title=title.strip(),
			type=chat.get("type"),
		)
		if telegram_bot:
			doc.append("bots", {"telegram_bot": telegram_bot})
		if telegram_user:
			doc.append("users", {"telegram_user": telegram_user})
		doc.insert(ignore_permissions=True)

		return doc

	doc = frappe.get_doc("Telegram Chat", name)
	changed = False

	for table, field, value in (
		("bots", "telegram_bot", telegram_bot),
		("users", "telegram_user", telegram_user),
	):
		if not value:
			continue
		if any(row.get(field) == value for row in doc.get(table)):
			continue
		doc.append(table, {field: value})
		changed = True

	if changed:
		doc.save(ignore_permissions=True)

	return doc
