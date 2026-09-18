"""
Pre-processor: заводит Telegram User / Telegram Chat / Telegram Message на каждый
входящий апдейт и складывает их в контекст, чтобы обработчикам не пришлось
делать это самим.
"""

from datetime import UTC, datetime

import frappe

from habibi_telegram.habibi_telegram.doctype.telegram_chat import telegram_chat as chat_store
from habibi_telegram.habibi_telegram.doctype.telegram_user import telegram_user as user_store
from habibi_telegram.mtproto import to_system_datetime
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

	media = media_info(message)

	doc = frappe.get_doc(
		doctype="Telegram Message",
		chat=context.telegram_chat.name,
		message_id=str(message["message_id"]),
		content=message.get("text") or message.get("caption") or media.get("label"),
		from_user=context.telegram_user.name,
		direction="Incoming",
		media_type=media.get("type"),
		media_file_id=media.get("file_id"),
		telegram_bot=context.telegram_bot.name,
		sent_on=_sent_on(message),
	)
	doc.insert(ignore_permissions=True)

	notify_new_message(doc, context.telegram_chat, telegram_bot=context.telegram_bot.name)

	return doc


def log_outgoing_message(telegram_bot: str, result, automated: bool = False):
	"""
	result — объект Message, который вернул sendMessage / sendDocument.

	automated — отправил не человек. Ответы обработчиков внутри process_update
	автоматические всегда, поэтому флаг апдейта учитывается здесь, а не у
	каждого вызывающего.
	"""
	if not isinstance(result, dict) or not result.get("message_id"):
		return None

	chat = chat_store.get_or_create(result.get("chat"), telegram_bot=telegram_bot)
	if not chat:
		return None

	media = media_info(result)

	if result.get("text"):
		content = result["text"]
	elif result.get("document"):
		content = "Sent file: " + (result["document"].get("file_name") or "")
	else:
		# Пометки те же, что у записей от личного аккаунта, — история общая
		content = result.get("caption") or media.get("label") or ""

	doc = frappe.get_doc(
		doctype="Telegram Message",
		chat=chat.name,
		message_id=str(result["message_id"]),
		content=content,
		from_bot=telegram_bot,
		direction="Outgoing",
		media_type=media.get("type"),
		media_file_id=media.get("file_id"),
		telegram_bot=telegram_bot,
		sent_on=_sent_on(result),
		is_automated=1 if (automated or frappe.flags.in_telegram_update) else 0,
	)
	doc.insert(ignore_permissions=True)

	return doc


# Вложения Bot API: у каждого свой ключ в сообщении, порядок — от частного к
# общему, потому что документом Telegram называет заодно и видео, и стикер
BOT_MEDIA_FIELDS = (
	"voice",
	"photo",
	"video_note",
	"animation",
	"sticker",
	"video",
	"audio",
	"document",
)


def media_info(message: dict) -> dict:
	"""Что за вложение и по какому file_id его потом забрать."""
	for field in BOT_MEDIA_FIELDS:
		value = message.get(field)
		if not value:
			continue

		if field == "photo":
			# Фотография приходит лесенкой размеров; нужен самый крупный
			value = max(value, key=lambda size: size.get("file_size") or 0)

		return {"type": field, "file_id": value.get("file_id"), "label": f"[{field}]"}

	return {}


def _sent_on(message: dict):
	"""Дата сообщения Bot API (unix-время UTC) → дата сайта.

	Без неё нельзя отличить свежее сообщение от доставленного с опозданием, а
	отвечать на вчерашнее автоматике нельзя.
	"""
	if not message.get("date"):
		return None
	return to_system_datetime(datetime.fromtimestamp(message["date"], tz=UTC))
