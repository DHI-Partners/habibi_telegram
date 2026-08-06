"""
HTTP-точки входа приложения.

Первая половина файла — вебхук, в который Telegram шлёт апдейты. Вместо
долгоживущего процесса с polling'ом — обычный whitelisted-метод Frappe.
Контейнеры остаются одноразовыми, ничего не надо держать запущенным, а
масштабируется это вместе с обычными веб-воркерами.

Адрес вебхука: /api/method/habibi_telegram.api.webhook?bot=<имя бота>

Вторая половина — то, чем живёт консоль чатов (/app/telegram-chat-app):
список диалогов, история и отправка.
"""

import hmac
import json

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password

from habibi_telegram.constants import SECRET_TOKEN_HEADER


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(bot: str = None, **kwargs):
	"""
	Принять апдейт от Telegram.

	Метод открыт для гостей — Telegram не умеет авторизоваться в Frappe.
	Вместо этого запрос подписан секретом, который мы отдали при setWebhook,
	и приходит в заголовке X-Telegram-Bot-Api-Secret-Token.
	"""
	telegram_bot = _authenticate(_bot_from_request(bot))
	update = _parse_update()

	from habibi_telegram.dispatcher import process_update

	try:
		process_update(telegram_bot, update)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title=f"Telegram update failed ({telegram_bot})",
			message=f"{frappe.get_traceback()}\n\nUpdate:\n{json.dumps(update, indent=2, ensure_ascii=False)}",
		)
		frappe.db.commit()

	# Telegram повторяет апдейт, пока не увидит 200. Ошибка обработки — наша
	# проблема, а не повод получить тот же битый апдейт ещё десять раз.
	frappe.local.response["http_status_code"] = 200

	return "ok"


def _bot_from_request(bot: str = None) -> str:
	"""
	Имя бота живёт в query-строке.

	Читаем его напрямую из request.args, а не из аргумента метода: при
	Content-Type: application/json frappe подменяет form_dict телом запроса,
	и параметры из URL до обработчика не доходят. Telegram всегда шлёт JSON.
	"""
	if frappe.request:
		return frappe.request.args.get("bot") or bot

	return bot


def _authenticate(bot: str) -> str:
	if not bot or not frappe.db.exists("Telegram Bot", bot):
		# Не подсказываем, какие боты существуют
		raise frappe.PermissionError

	expected = get_decrypted_password(
		"Telegram Bot", bot, "webhook_secret", raise_exception=False
	)
	provided = frappe.get_request_header(SECRET_TOKEN_HEADER) or ""

	if not expected or not hmac.compare_digest(str(expected), str(provided)):
		raise frappe.PermissionError

	return bot


def _parse_update() -> dict:
	raw = frappe.request.get_data(as_text=True) if frappe.request else ""

	try:
		update = json.loads(raw or "{}")
	except ValueError:
		frappe.throw(_("Malformed update payload"), frappe.ValidationError)

	if not isinstance(update, dict):
		frappe.throw(_("Malformed update payload"), frappe.ValidationError)

	return update


# ---------------------------------------------------------------------------
# Консоль чатов
#
# Три метода, на которых держится страница /app/telegram-chat-app. Ничего
# своего они не делают: чаты и сообщения уже лежат в базе, отправка живёт в
# habibi_telegram.client. Здесь только выборки в том виде, в каком их удобно
# рисовать, и один общий формат сообщения для истории, отправки и realtime.
# ---------------------------------------------------------------------------

# Сколько строк отдаём за раз, если вызывающий не попросил иначе
CHATS_PAGE_LENGTH = 50
MESSAGES_PAGE_LENGTH = 50

# Потолок на limit: страница листает историю сама, гнать её тысячами незачем
MAX_PAGE_LENGTH = 200

# Длина превью последнего сообщения в списке диалогов
PREVIEW_LENGTH = 120

# Подписи в выпадающем списке форматирования → parse_mode Telegram
TEXT_FORMATS = {
	"Plain text": None,
	"HTML": "HTML",
	"MarkdownV2": "MarkdownV2",
	"Markdown": "Markdown",
}


@frappe.whitelist()
def get_chats(search: str = None, limit: int = CHATS_PAGE_LENGTH, start: int = 0) -> list[dict]:
	"""
	Диалоги для левой колонки — свежие сверху.

	Последнее сообщение не ищется запросом по истории: Telegram Message при
	вставке сам обновляет last_message_content и last_message_on у чата, так
	что список стоит одного SELECT'а независимо от объёма переписки.
	"""
	or_filters = None
	if search:
		like = f"%{search}%"
		or_filters = [["title", "like", like], ["chat_id", "like", like]]

	chats = frappe.get_list(
		"Telegram Chat",
		fields=["name", "title", "type", "chat_id", "last_message_on", "last_message_content"],
		or_filters=or_filters,
		# NULL в MariaDB меньше любой даты, поэтому чаты без сообщений
		# сами уезжают вниз списка
		order_by="last_message_on desc, modified desc",
		limit_page_length=_page_length(limit),
		limit_start=cint(start),
	)

	for chat in chats:
		chat["last_message"] = _preview(chat.pop("last_message_content", None))
		chat["last_message_on"] = _timestamp(chat.get("last_message_on"))

	return chats


@frappe.whitelist()
def get_messages(
	chat_id: str, limit: int = MESSAGES_PAGE_LENGTH, start: int = 0, after: str = None
) -> list[dict]:
	"""
	История одного чата, от старых к новым.

	chat_id — имя документа Telegram Chat; оно же и есть идентификатор чата в
	Telegram (autoname: field:chat_id).

	start листает вглубь: 0 — последняя страница переписки, 50 — предыдущая.
	after отдаёт только то, что появилось после указанного creation, — на нём
	держится поллинг, когда socket.io недоступен.
	"""
	frappe.has_permission("Telegram Chat", "read", doc=chat_id, throw=True)

	filters = {"chat": chat_id}
	if after:
		filters["creation"] = (">", after)

	rows = frappe.get_list(
		"Telegram Message",
		fields=[
			"name",
			"chat",
			"message_id",
			"direction",
			"content",
			"from_user",
			"from_bot",
			"telegram_account",
			"sent_on",
			"creation",
			"is_edited",
			"is_deleted",
		],
		filters=filters,
		# Листаем по creation: у сообщений от ботов sent_on пустой, а порядок
		# страниц должен быть устойчивым
		order_by="creation desc",
		limit_page_length=_page_length(limit),
		limit_start=cint(start),
	)

	senders = _sender_names(rows)

	# Внутри страницы показываем по времени Telegram: аккаунт может принести
	# старую переписку разом, и creation тогда ничего не значит
	rows.sort(key=lambda row: row.get("sent_on") or row.get("creation"))

	return [message_payload(row, senders) for row in rows]


@frappe.whitelist(methods=["POST"])
def send_chat_message(
	chat_id: str,
	content: str,
	text_format: str = "Plain text",
	bot: str = None,
	account: str = None,
) -> dict:
	"""
	Отправить сообщение в чат и вернуть записанное Telegram Message.

	Отправитель можно не указывать: в чат, где есть бот, пишет бот, в остальные
	(личная переписка двух людей боту недоступна) — личный аккаунт.
	"""
	from habibi_telegram.client import send_chat_message as send

	content = (content or "").strip()
	if not content:
		frappe.throw(_("Cannot send an empty Telegram message"))

	chat = frappe.get_doc("Telegram Chat", chat_id)
	chat.check_permission("write")

	if not bot and not account:
		bot, account = _pick_transport(chat)

	result = send(
		chat=chat.name,
		message=content,
		parse_mode=_parse_mode(text_format),
		from_bot=bot,
		from_account=account,
	)

	return message_payload(_sent_message(chat, result))


def message_payload(row, sender_names: dict = None) -> dict:
	"""
	Один формат сообщения на всех: история, ответ на отправку и realtime.

	row — строка из get_list или сам документ Telegram Message; и то, и другое
	отвечает на .get().
	"""
	sender_names = sender_names or {}
	from_user = row.get("from_user")

	sender = sender_names.get(from_user) if from_user else None
	if from_user and sender is None:
		sender = frappe.db.get_value("Telegram User", from_user, "full_name")

	return {
		"name": row.get("name"),
		"chat": row.get("chat"),
		"message_id": row.get("message_id"),
		"direction": row.get("direction") or "Incoming",
		"content": row.get("content") or "",
		"sender": sender or row.get("from_bot") or row.get("telegram_account") or "",
		# Что показать под пузырём: время Telegram, если оно известно
		"timestamp": _timestamp(row.get("sent_on") or row.get("creation")),
		# Курсор поллинга — только creation, иначе догрузка пропустит
		# подтянутую задним числом историю
		"creation": _timestamp(row.get("creation")),
		"is_edited": cint(row.get("is_edited")),
		"is_deleted": cint(row.get("is_deleted")),
	}


def _page_length(limit) -> int:
	return max(1, min(cint(limit) or MESSAGES_PAGE_LENGTH, MAX_PAGE_LENGTH))


def _preview(content: str) -> str:
	preview = (content or "").strip().replace("\n", " ")

	if len(preview) > PREVIEW_LENGTH:
		preview = preview[: PREVIEW_LENGTH - 1] + "…"

	return preview


def _timestamp(value) -> str | None:
	"""Микросекунды не отбрасываем: по creation листается поллинг."""
	return str(value) if value else None


def _sender_names(rows: list) -> dict:
	"""Имена авторов одним запросом, а не по строке на сообщение."""
	names = {row.get("from_user") for row in rows if row.get("from_user")}
	if not names:
		return {}

	return dict(
		frappe.get_all(
			"Telegram User",
			filters={"name": ("in", list(names))},
			fields=["name", "full_name"],
			as_list=True,
		)
	)


def _parse_mode(text_format: str) -> str | None:
	if text_format in (None, ""):
		return None

	if text_format not in TEXT_FORMATS:
		frappe.throw(
			_("Unknown text format '{0}'. Use one of: {1}").format(
				text_format, ", ".join(TEXT_FORMATS)
			)
		)

	return TEXT_FORMATS[text_format]


def _pick_transport(chat) -> tuple[str | None, str | None]:
	"""(бот, аккаунт) — кем писать в этот чат, если выбор не сделали за нас."""
	if chat.bots:
		return chat.bots[0].telegram_bot, None

	account = chat.get_account()
	if account:
		return None, account

	# Ни бота, ни аккаунта в чате нет — пусть client возьмёт основного бота и
	# сам объяснит, если и его не назначили
	return None, None


def _sent_message(chat, result):
	"""
	Найти запись, которую отправка завела в истории.

	Личный аккаунт возвращает имя документа, бот — объект Message из ответа
	Telegram; сама запись в обоих случаях уже сделана (см. handlers.logging).
	Длинное сообщение бот режет на части, и result — последняя из них.
	"""
	name = None

	if isinstance(result, str):
		name = result
	elif isinstance(result, dict) and result.get("message_id"):
		name = frappe.db.get_value(
			"Telegram Message",
			{"chat": chat.name, "message_id": str(result["message_id"])},
		)

	if name:
		return frappe.get_doc("Telegram Message", name)

	# Сообщение ушло, а записать его не удалось. Молчать нельзя — страница
	# ждёт пузырь, — поэтому отдаём то, что знаем о нём сами
	return frappe._dict(
		chat=chat.name,
		message_id=str((result or {}).get("message_id") or ""),
		direction="Outgoing",
		content=(result or {}).get("text") or "",
		creation=frappe.utils.now_datetime(),
	)
