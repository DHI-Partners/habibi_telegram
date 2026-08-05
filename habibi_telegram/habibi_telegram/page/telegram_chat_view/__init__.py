import frappe
from frappe import _
from frappe.utils import cint


@frappe.whitelist()
def get_telegram_chat(chat_type: str, user: str = None, group: str = None):
	chat_type = (chat_type or "").lower()

	if chat_type == "private":
		telegram_user_id = frappe.db.get_value("Telegram User", {"user": user}, "telegram_user_id")
		if not telegram_user_id:
			frappe.throw(_("No Telegram User account exists for user: {0}").format(user))

		# В личных чатах chat_id совпадает с telegram_user_id
		return telegram_user_id

	if chat_type == "group":
		if not frappe.db.exists("Telegram Chat", group):
			frappe.throw(_("Unknown Chat: {0}").format(group))

		return group

	frappe.throw(_("Unknown chat type: {0}").format(chat_type))


@frappe.whitelist()
def load_chat_rooms(limit_start=0, limit_page_length=20):
	return frappe.get_all(
		"Telegram Chat",
		fields=["chat_id", "title", "type", "last_message_on", "last_message_content"],
		order_by="last_message_on desc",
		limit_start=cint(limit_start),
		limit_page_length=cint(limit_page_length),
	)


@frappe.whitelist()
def load_chat_messages(chat_id: str, limit_start=0, limit_page_length=20):
	messages = frappe.get_all(
		"Telegram Message",
		filters={"chat": chat_id},
		fields=["name", "content", "from_user", "from_bot", "message_id", "creation"],
		order_by="creation desc",
		limit_start=cint(limit_start),
		limit_page_length=cint(limit_page_length),
	)

	# В интерфейсе сверху старые, снизу свежие
	return list(reversed(messages))
