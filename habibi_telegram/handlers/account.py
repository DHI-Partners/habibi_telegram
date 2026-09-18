"""
Запись того, что видит личный аккаунт, в Telegram Chat / User / Message.

Апдейты от MTProto приходят в другой форме, чем от Bot API, но история должна
получиться общая: один и тот же чат обязан иметь один и тот же chat_id
независимо от того, увидел его бот или аккаунт. За это отвечает `mtproto.peer_id`,
а здесь объекты Telethon превращаются в документы.

Сюда попадает всё, что доехало до аккаунта: личные переписки, группы, каналы,
сообщения ботов и служебные уведомления самого Telegram (чат 777000).
"""

import frappe

from habibi_telegram import mtproto
from habibi_telegram.habibi_telegram.doctype.telegram_chat import telegram_chat as chat_store
from habibi_telegram.habibi_telegram.doctype.telegram_user import telegram_user as user_store
from habibi_telegram.notifications import notify_new_message

# Медиа без подписи: в списке сообщений лучше пометка, чем пустая строка
MEDIA_LABELS = {
	"MessageMediaPhoto": "[photo]",
	"MessageMediaDocument": "[document]",
	"MessageMediaGeo": "[location]",
	"MessageMediaGeoLive": "[live location]",
	"MessageMediaContact": "[contact]",
	"MessageMediaPoll": "[poll]",
	"MessageMediaVenue": "[venue]",
	"MessageMediaWebPage": "",
	"MessageMediaDice": "[dice]",
	"MessageMediaInvoice": "[invoice]",
	"MessageMediaGame": "[game]",
}


def log_message(
	account, message, entities: dict = None, notify: bool = True, automated: bool = False
) -> tuple[str | None, bool]:
	"""
	Записать сообщение аккаунта.

	Возвращает (имя документа Telegram Message, завели ли его сейчас) — второе
	нужно, чтобы счётчики синхронизации не считали повторные апдейты новыми.

	message — объект Message или MessageService из Telethon,
	entities — индекс сущностей из того же ответа (см. mtproto.index_entities),
	notify — оповещать ли в колокольчике; на разборе старой истории выключается.
	automated — отправил не человек (передаёт тот, кто отправлял через user_client).
	"""
	entities = entities or {}

	chat_id = mtproto.peer_id(getattr(message, "peer_id", None))
	message_id = getattr(message, "id", None)
	if chat_id is None or not message_id:
		return None, False

	chat = chat_store.get_or_create(
		_chat_dict(chat_id, entities), telegram_account=account.name
	)
	if not chat:
		return None, False

	sender = _sender_dict(account, message, entities)
	telegram_user = user_store.get_or_create(sender) if sender else None

	existing = frappe.db.get_value(
		"Telegram Message",
		{"chat": chat.name, "message_id": str(message_id)},
	)
	if existing:
		# Один и тот же апдейт может приехать дважды: разница по pts перекрывает
		# уже прочитанное, а слушатель работает параллельно с фоновой задачей.
		# Слушатель может записать и наше же исходящее раньше отправителя —
		# тогда пометку «не человек» ставим на уже записанное, иначе она
		# потерялась бы и сообщение считалось бы ручным
		if automated:
			frappe.db.set_value("Telegram Message", existing, "is_automated", 1, update_modified=False)
		return existing, False

	doc = frappe.get_doc(
		doctype="Telegram Message",
		chat=chat.name,
		message_id=str(message_id),
		content=message_content(message),
		from_user=telegram_user.name if telegram_user else None,
		telegram_account=account.name,
		direction="Outgoing" if getattr(message, "out", False) else "Incoming",
		sent_on=mtproto.to_system_datetime(getattr(message, "date", None)),
		# file_id здесь не бывает: в MTProto вложение живёт только вместе со
		# своим сообщением, и качается оно по номеру сообщения
		media_type=media_kind(message),
		is_automated=1 if automated else 0,
	)
	doc.insert(ignore_permissions=True)

	if notify:
		notify_new_message(doc, chat, telegram_account=account.name)

	return doc.name, True


def mark_edited(account, message, entities: dict = None) -> str | None:
	"""Правка сообщения: содержимое обновляем, историю не плодим."""
	entities = entities or {}

	chat_id = mtproto.peer_id(getattr(message, "peer_id", None))
	message_id = getattr(message, "id", None)
	if chat_id is None or not message_id:
		return None

	name = _find_message(chat_id, message_id)
	if not name:
		# Правку увидели, а самого сообщения не видели — запишем как новое
		return log_message(account, message, entities)[0]

	doc = frappe.get_doc("Telegram Message", name)
	doc.db_set(
		{
			"content": message_content(message),
			"is_edited": 1,
			"edited_on": mtproto.to_system_datetime(getattr(message, "edit_date", None))
			or frappe.utils.now_datetime(),
		},
		update_modified=False,
	)
	doc.update_chat_preview()

	return name


def mark_deleted(account, message_ids: list, chat_id=None) -> int:
	"""
	Удаление на стороне Telegram: помечаем, но из базы не выносим.

	В личных переписках и обычных группах MTProto не сообщает, из какого чата
	удалили — только идентификаторы сообщений. Тогда ищем среди чатов этого
	аккаунта: идентификаторы там сквозные по диалогу, так что чужое зацепить
	можно только теоретически.
	"""
	if not message_ids:
		return 0

	ids = [str(x) for x in message_ids]

	if chat_id is not None:
		chat = frappe.db.get_value("Telegram Chat", {"chat_id": str(chat_id)})
		if not chat:
			return 0
		chats = [chat]
	else:
		chats = _account_chats(account)

	if not chats:
		return 0

	names = frappe.get_all(
		"Telegram Message",
		filters={"chat": ("in", chats), "message_id": ("in", ids), "is_deleted": 0},
		pluck="name",
	)

	for name in names:
		frappe.db.set_value(
			"Telegram Message",
			name,
			{"is_deleted": 1, "deleted_on": frappe.utils.now_datetime()},
			update_modified=False,
		)

	return len(names)


def message_content(message) -> str:
	"""Текст сообщения, а для медиа и служебных событий — короткая пометка."""
	text = (getattr(message, "message", None) or "").strip()
	if text:
		return text

	action = getattr(message, "action", None)
	if action is not None:
		return "[{0}]".format(type(action).__name__.replace("MessageAction", "").lower())

	media = getattr(message, "media", None)
	if media is not None:
		kind = media_kind(message)
		if kind:
			return f"[{kind}]"

		label = MEDIA_LABELS.get(type(media).__name__)

		return label if label is not None else "[media]"

	return ""


def media_kind(message) -> str | None:
	"""
	Что за вложение: voice, photo, document…

	Документом MTProto называет всё подряд — голосовое, видео, «кружок»,
	стикер, — и различаются они только атрибутами. Названия сведены к тем же,
	что у Bot API: история общая, и разбирать её потом должно одно и то же
	место.
	"""
	media = getattr(message, "media", None)
	if media is None:
		return None

	name = type(media).__name__

	if name == "MessageMediaPhoto":
		return "photo"

	if name != "MessageMediaDocument":
		return None

	document = getattr(media, "document", None)
	attributes = [type(a).__name__ for a in getattr(document, "attributes", None) or []]

	for attribute in getattr(document, "attributes", None) or []:
		if getattr(attribute, "voice", False):
			return "voice"

		if getattr(attribute, "round_message", False):
			return "video_note"

	if "DocumentAttributeSticker" in attributes:
		return "sticker"

	if "DocumentAttributeAnimated" in attributes:
		return "animation"

	if "DocumentAttributeVideo" in attributes:
		return "video"

	if "DocumentAttributeAudio" in attributes:
		return "audio"

	return "document"


# -- вспомогательное ---------------------------------------------------------


def _chat_dict(chat_id: int, entities: dict) -> dict:
	entity = entities.get(chat_id)
	chat = mtproto.entity_to_chat(entity) if entity is not None else None

	# Сущности в ответе может не оказаться — чат всё равно нужен, пусть и без
	# названия: при следующем сообщении Telegram пришлёт её и имя подтянется
	return chat or {"id": chat_id, "title": str(chat_id)}


def _sender_dict(account, message, entities: dict) -> dict | None:
	from_id = getattr(message, "from_id", None)

	if from_id is not None:
		sender_id = mtproto.peer_id(from_id)
	elif getattr(message, "out", False):
		sender_id = frappe.utils.cint(account.account_id) or None
	else:
		# Личная переписка: отправитель — сам собеседник, отдельного from_id нет
		peer = getattr(message, "peer_id", None)
		sender_id = mtproto.peer_id(peer) if peer is not None else None
		if sender_id is not None and sender_id < 0:
			# Не личка, а группа без from_id (анонимный админ, пост канала)
			sender_id = None

	if sender_id is None:
		return None

	entity = entities.get(sender_id)
	user = mtproto.entity_to_user(entity) if entity is not None else None

	return user or {"id": sender_id}


def _find_message(chat_id: int, message_id) -> str | None:
	chat = frappe.db.get_value("Telegram Chat", {"chat_id": str(chat_id)})
	if not chat:
		return None

	return frappe.db.get_value(
		"Telegram Message", {"chat": chat, "message_id": str(message_id)}
	)


def _account_chats(account) -> list:
	return frappe.get_all(
		"Telegram Account Item",
		filters={"telegram_account": account.name, "parenttype": "Telegram Chat"},
		pluck="parent",
		parent_doctype="Telegram Chat",
		ignore_permissions=True,
	)
