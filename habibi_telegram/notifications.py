"""
Оповещения о новых входящих сообщениях — колокольчик в интерфейсе Frappe.

Синхронизация складывает сообщения в базу молча: чтобы их увидеть, надо было
самому открыть список. Здесь на каждое входящее заводится Notification Log —
стандартный механизм Frappe, тот самый, что показывает счётчик в шапке и
всплывающее уведомление на открытой вкладке.

Два правила, без которых колокольчик стал бы бесполезным:

  - Одно уведомление на чат, пока предыдущее не прочитано. В болтливой группе
    иначе накопилось бы двести штук за вечер.
  - Ничего не шлём про старые сообщения. Первый разбор истории — это сотни
    сообщений за годы, а после простоя сайта разница приезжает пачкой.
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, time_diff_in_seconds

# Старше этого — уже не новость, а история
MAX_AGE_SECONDS = 15 * 60

# Сколько текста показать в уведомлении
PREVIEW_LENGTH = 140

# Потолок получателей: роль может оказаться надетой на половину сайта
MAX_RECIPIENTS = 20


def notify_new_message(doc, chat, telegram_account=None, telegram_bot=None):
	"""
	Оповестить о входящем сообщении.

	doc   — Telegram Message, уже записанный,
	chat  — Telegram Chat, куда оно пришло,
	дальше — источник: личный аккаунт либо бот.
	"""
	if doc.direction != "Incoming":
		return None

	if _is_stale(doc):
		return None

	settings = _settings(telegram_account=telegram_account, telegram_bot=telegram_bot)
	if not settings:
		return None

	if not _wanted_chat_type(chat, settings):
		return None

	created = []
	for user in settings["users"]:
		if _has_unread(user, chat):
			continue

		created.append(_insert_log(user, doc, chat))

	return created


def _is_stale(doc) -> bool:
	sent = doc.sent_on or doc.creation or now_datetime()

	return time_diff_in_seconds(now_datetime(), sent) > MAX_AGE_SECONDS


def _settings(telegram_account=None, telegram_bot=None) -> dict | None:
	"""Кого оповещать и о каких чатах — по настройкам источника сообщения."""
	if telegram_account:
		source = _load(telegram_account, "Telegram Account")
	elif telegram_bot:
		source = _load(telegram_bot, "Telegram Bot")
	else:
		return None

	if not source or not cint(source.notify_on_new_message):
		return None

	users = recipients(source)
	if not users:
		# Получателя не назначили — уведомлять некого. На форме об этом
		# написано, чтобы включённая галочка не выглядела поломкой
		return None

	return {
		"users": users,
		"private": cint(source.notify_private),
		"groups": cint(source.notify_groups),
		"channels": cint(source.notify_channels),
	}


def recipients(source) -> list:
	"""
	Кому уходит уведомление.

	Указан получатель — ему. Не указан — владельцу личного аккаунта (у бота
	владельца нет, поэтому там только явный получатель). Роль добавляет к этому
	всех, кто её носит, — так настраивают общий рабочий аккаунт, за которым
	смотрит отдел.

	Administrator не подставляется сам никогда: это служебный логин, в
	интерфейсе под ним не сидят, и уведомление ушло бы в пустоту. Выбрать его
	руками в Notify User по-прежнему можно.
	"""
	users = []

	if source.get("notify_user"):
		users.append(source.notify_user)
	elif source.get("user"):
		users.append(source.user)

	if source.get("notify_role"):
		by_role = frappe.get_all(
			"Has Role",
			filters={"role": source.notify_role, "parenttype": "User"},
			pluck="parent",
		)
		# Administrator носит все роли сразу, и раздача по роли всегда цепляла бы
		# его — а это ровно тот случай, когда его выбирали не нарочно
		users.extend(u for u in by_role if u != "Administrator")

	seen = []
	for user in users:
		if user and user not in seen and user != "Guest":
			seen.append(user)

	enabled = [
		user
		for user in seen
		if frappe.db.get_value("User", user, "enabled")
		and frappe.db.get_value("User", user, "user_type") == "System User"
	]

	# Роль вроде System Manager может оказаться на половине сайта; сотня
	# одинаковых записей в колокольчике никому не поможет
	return enabled[:MAX_RECIPIENTS]


def _load(name: str, doctype: str):
	fields = [
		"name",
		"notify_on_new_message",
		"notify_user",
		"notify_role",
		"notify_private",
		"notify_groups",
		"notify_channels",
	]
	if doctype == "Telegram Account":
		fields.append("user")

	return frappe.db.get_value(doctype, name, fields, as_dict=True)


def _wanted_chat_type(chat, settings: dict) -> bool:
	chat_type = (chat.type or "private").lower()

	if chat_type == "private":
		return bool(settings["private"])

	if chat_type == "channel":
		return bool(settings["channels"])

	# group и supergroup — одно и то же с точки зрения человека
	return bool(settings["groups"])


def _has_unread(user: str, chat) -> bool:
	"""Непрочитанное уведомление по этому чату уже висит — второго не нужно."""
	return bool(
		frappe.db.exists(
			"Notification Log",
			{
				"for_user": user,
				"document_type": "Telegram Chat",
				"document_name": chat.name,
				"read": 0,
			},
		)
	)


def _insert_log(user: str, doc, chat):
	preview = (doc.content or "").strip().replace("\n", " ")
	if len(preview) > PREVIEW_LENGTH:
		preview = preview[: PREVIEW_LENGTH - 1] + "…"

	log = frappe.get_doc(
		doctype="Notification Log",
		for_user=user,
		type="Alert",
		subject=_("New Telegram message from {0}").format(frappe.utils.escape_html(chat.title)),
		email_content=frappe.utils.escape_html(preview),
		# Ведём на чат, а не на отдельное сообщение: там вся переписка и кнопка
		# ответа
		document_type="Telegram Chat",
		document_name=chat.name,
	)
	log.insert(ignore_permissions=True)

	return log.name
