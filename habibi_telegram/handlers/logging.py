"""
Pre-processor: заводит Telegram User / Telegram Chat / Telegram Message на каждый
входящий апдейт и складывает их в контекст, чтобы обработчикам не пришлось
делать это самим.
"""

import frappe

from habibi_telegram.habibi_telegram.doctype.telegram_chat import telegram_chat as chat_store
from habibi_telegram.habibi_telegram.doctype.telegram_user import telegram_user as user_store
from habibi_telegram.notifications import notify_new_message
from habibi_telegram.utils import update as u


def pre_process(context):
	update = context.update

	telegram_user = user_store.get_or_create(u.effective_user(update))
	if not telegram_user:
		return

	context.telegram_user = telegram_user
	context.telegram_chat = chat_store.get_or_create(
		u.effective_chat(update),
		telegram_bot=context.telegram_bot.name,
		telegram_user=telegram_user.name,
	)

	if context.telegram_chat:
		context.telegram_message = log_incoming_message(context)


def log_incoming_message(context):
	"""
	Пишем в историю только настоящие входящие сообщения. У callback_query
	effective_message — это сообщение с кнопкой, которое мы уже записали,
	когда отправляли.
	"""
	update = context.update
	message = update.get("message") or update.get("edited_message")
	if not message or not message.get("message_id"):
		return None

	existing = frappe.db.get_value(
		"Telegram Message",
		{"chat": context.telegram_chat.name, "message_id": str(message["message_id"])},
	)
	if existing:
		return frappe.get_doc("Telegram Message", existing)

	doc = frappe.get_doc(
		doctype="Telegram Message",
		chat=context.telegram_chat.name,
		message_id=str(message["message_id"]),
		content=message.get("text") or message.get("caption"),
		from_user=context.telegram_user.name,
		direction="Incoming",
	)
	doc.insert(ignore_permissions=True)

	notify_new_message(doc, context.telegram_chat, telegram_bot=context.telegram_bot.name)

	return doc


def log_outgoing_message(telegram_bot: str, result):
	"""result — объект Message, который вернул sendMessage / sendDocument."""
	if not isinstance(result, dict) or not result.get("message_id"):
		return None

	chat = chat_store.get_or_create(result.get("chat"), telegram_bot=telegram_bot)
	if not chat:
		return None

	if result.get("text"):
		content = result["text"]
	elif result.get("document"):
		content = "Sent file: " + (result["document"].get("file_name") or "")
	else:
		content = result.get("caption") or ""

	doc = frappe.get_doc(
		doctype="Telegram Message",
		chat=chat.name,
		message_id=str(result["message_id"]),
		content=content,
		from_bot=telegram_bot,
		direction="Outgoing",
	)
	doc.insert(ignore_permissions=True)

	return doc
