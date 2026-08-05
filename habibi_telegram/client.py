"""
Публичный API приложения: отправка сообщений и файлов из кода, хуков и
контроллеров. Всё, что снаружи хочет «написать в телеграм», ходит сюда.

	from habibi_telegram.client import send_message
	send_message("Заказ SO-0042 оплачен", user="manager@example.com")
"""

import os

import frappe
from frappe import _
from frappe.utils.jinja import render_template

from habibi_telegram.constants import DEFAULT_TELEGRAM_BOT_KEY
from habibi_telegram.telegram_api import (
	MAX_MESSAGE_LENGTH,
	ParseMode,  # noqa: F401 — реэкспорт, им пользуются снаружи
	TelegramBotAPI,
)
from habibi_telegram.utils.formatting import strip_unsupported_html_tags


def send_message(
	message_text: str,
	parse_mode: str = None,
	user: str = None,
	telegram_user: str = None,
	from_bot: str = None,
	chat_id=None,
	reply_markup=None,
):
	"""
	Отправить сообщение пользователю Telegram.

	message_text: текст, 1–4096 символов
	parse_mode:   ParseMode.HTML / MARKDOWN_V2 / MARKDOWN либо None
	user:         пользователь Frappe — telegram_user найдётся по связи
	telegram_user: имя документа Telegram User, если пользователь Frappe не нужен
	from_bot:     имя Telegram Bot; по умолчанию — бот, помеченный как основной
	chat_id:      отправить прямо в чат, минуя Telegram User (для групп)
	"""
	message_text = sanitize_message_text(message_text, parse_mode)
	if not message_text:
		frappe.throw(_("Cannot send an empty Telegram message"))

	if chat_id is None:
		chat_id = get_telegram_user_id(user=user, telegram_user=telegram_user)

	from_bot = from_bot or get_default_bot()
	bot = get_bot(from_bot)

	result = None
	for chunk in _split_message(message_text, parse_mode):
		result = bot.send_message(
			chat_id, text=chunk, parse_mode=parse_mode, reply_markup=reply_markup
		)
		log_outgoing_message(telegram_bot=from_bot, result=result)

	return result


def send_file(
	file,
	filename: str = None,
	message: str = None,
	parse_mode: str = None,
	user: str = None,
	telegram_user: str = None,
	from_bot: str = None,
	chat_id=None,
):
	"""
	Отправить файл.

	file: документ File, внутренний путь (/files/x.pdf, /private/files/x.pdf),
	      публичный URL, file_id Telegram, bytes или открытый файл
	message: подпись к файлу, 0–1024 символа
	"""
	message = sanitize_message_text(message, parse_mode)

	if chat_id is None:
		chat_id = get_telegram_user_id(user=user, telegram_user=telegram_user)

	from_bot = from_bot or get_default_bot()

	file, filename = _resolve_file(file, filename)

	bot = get_bot(from_bot)
	result = bot.send_document(
		chat_id, document=file, filename=filename, caption=message, parse_mode=parse_mode
	)
	log_outgoing_message(telegram_bot=from_bot, result=result)

	return result


@frappe.whitelist()
def send_chat_message(chat: str, message: str, from_bot: str = None, parse_mode: str = None):
	"""
	Отправить сообщение в чат из интерфейса.

	Права проверяем по самому чату: писать в него могут те же, кому доступна
	запись в Telegram Chat, то есть Telegram Bot Manager и System Manager.
	"""
	frappe.has_permission("Telegram Chat", "write", doc=chat, throw=True)

	chat_id = frappe.db.get_value("Telegram Chat", chat, "chat_id")
	if not chat_id:
		frappe.throw(_("Unknown chat: {0}").format(chat))

	return send_message(message, parse_mode=parse_mode, chat_id=chat_id, from_bot=from_bot)


@frappe.whitelist()
def send_message_from_template(
	template: str,
	context: dict = None,
	lang: str = None,
	parse_mode: str = None,
	user: str = None,
	telegram_user: str = None,
	from_bot: str = None,
):
	"""Отправить сообщение, собранное из Telegram Message Template."""
	frappe.only_for(("Telegram Bot Manager", "System Manager"))

	message = render_message_from_template(template, context=context, lang=lang)

	return send_message(
		message,
		parse_mode=parse_mode,
		user=user,
		telegram_user=telegram_user,
		from_bot=from_bot,
	)


def render_message_from_template(template: str, context: dict = None, lang: str = None) -> str:
	"""
	Собрать текст из Telegram Message Template.

	lang выбирает перевод из дочерней таблицы; если перевода нет — берётся
	default_template.
	"""
	if not frappe.db.exists("Telegram Message Template", template):
		frappe.throw(_("No template with name '{0}' exists.").format(template))

	template_doc = frappe.get_cached_doc("Telegram Message Template", template)
	body = ""

	if lang:
		for translation in template_doc.template_translations:
			if translation.language == lang:
				body = translation.template
				break

	if not body:
		body = template_doc.default_template

	return render_template(body, context or {})


# -- получатели и боты -------------------------------------------------------


def get_telegram_user_id(user: str = None, telegram_user: str = None):
	"""Найти telegram_user_id по пользователю Frappe или по документу Telegram User."""
	if not user and not telegram_user:
		frappe.throw(_("Please specify either frappe-user or telegram-user"))

	telegram_user_id = None

	if user:
		telegram_user_id = frappe.db.get_value(
			"Telegram User", {"user": user}, "telegram_user_id"
		)

	if telegram_user and not telegram_user_id:
		telegram_user_id = frappe.db.get_value("Telegram User", telegram_user, "telegram_user_id")

	if not telegram_user_id:
		frappe.throw(_("Telegram user does not exist"))

	return telegram_user_id


def get_default_bot() -> str:
	bot = frappe.db.get_default(DEFAULT_TELEGRAM_BOT_KEY)
	if not bot:
		frappe.throw(_("No default Telegram Bot is set. Open a Telegram Bot and press 'Mark as Default'."))

	return bot


def get_bot(telegram_bot: str = None) -> TelegramBotAPI:
	"""Клиент Bot API для указанного (или основного) бота."""
	telegram_bot = telegram_bot or get_default_bot()
	doc = frappe.get_cached_doc("Telegram Bot", telegram_bot)

	return TelegramBotAPI(token=doc.get_password("api_token"), bot_name=doc.name)


# -- подготовка текста -------------------------------------------------------


def validate_parse_mode(parse_mode: str) -> None:
	if parse_mode and parse_mode not in ParseMode.values():
		frappe.throw(
			_("Invalid parse mode '{0}'. Use one of: {1}").format(
				parse_mode, ", ".join(ParseMode.values())
			)
		)


def sanitize_message_text(message_text: str, parse_mode: str = None) -> str:
	"""HTML из шаблонов Frappe чистится, остальные режимы уходят как есть."""
	validate_parse_mode(parse_mode)

	if not message_text or not parse_mode:
		return message_text

	if parse_mode == ParseMode.HTML:
		return strip_unsupported_html_tags(message_text)

	return message_text


def _split_message(text: str, parse_mode: str = None):
	"""
	Telegram режет всё длиннее 4096 символов.

	Простой текст бьём по переносам строк. Размеченный не трогаем: разрез
	посреди тега сломает разметку, и лучше честно упасть, чем отправить мусор.
	"""
	if len(text) <= MAX_MESSAGE_LENGTH:
		return [text]

	if parse_mode:
		frappe.throw(
			_("Message is {0} characters long; Telegram allows {1}. Shorten it or drop parse_mode.").format(
				len(text), MAX_MESSAGE_LENGTH
			)
		)

	chunks = []
	remaining = text
	while remaining:
		if len(remaining) <= MAX_MESSAGE_LENGTH:
			chunks.append(remaining)
			break

		window = remaining[:MAX_MESSAGE_LENGTH]
		cut = window.rfind("\n")
		if cut <= 0:
			cut = MAX_MESSAGE_LENGTH

		chunks.append(remaining[:cut])
		remaining = remaining[cut:].lstrip("\n")

	return chunks


def _resolve_file(file, filename: str = None):
	"""Свести все допустимые виды `file` к тому, что понимает sendDocument."""
	from frappe.core.doctype.file.file import File

	if isinstance(file, File):
		file = file.file_url

	if isinstance(file, str) and "/files/" in file:
		# Внутренний файл сайта — читаем с диска, Telegram до него не достучится
		relative = ("" if "/private/" in file else "/public") + file
		file_path = frappe.get_site_path(relative.strip("/"))

		if os.path.exists(file_path):
			filename = filename or os.path.basename(file_path)
			with open(file_path, "rb") as f:
				file = f.read()

	return file, filename


def log_outgoing_message(telegram_bot: str, result):
	"""Отложенный импорт — handlers.logging тянет doctype-слой."""
	from habibi_telegram.handlers.logging import log_outgoing_message as _log

	return _log(telegram_bot=telegram_bot, result=result)
