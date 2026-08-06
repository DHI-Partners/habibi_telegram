"""
Личный аккаунт Telegram: MTProto вместо Bot API.

Bot API умеет только то, что разрешено ботам: он не видит переписку человека,
не получает служебные уведомления Telegram и не может писать от чужого имени.
Всё это — клиентский протокол MTProto, и единственная зрелая его реализация на
чистом Python — Telethon. Это первая внешняя зависимость приложения, и нужна
она только разделу «Telegram Account»: импорт ленивый, без Telethon остальное
работает как работало.

Вебхуков в MTProto нет. Апдейты либо валятся в открытое соединение, либо
добираются запросом updates.getDifference по сохранённому состоянию (pts/qts/
date). Мы выбрали второе: состояние лежит в документе Telegram Account, фоновая
задача поднимает соединение на пару секунд и кладёт трубку — держать процесс
запущенным по-прежнему не нужно. Кому нужен realtime, есть отдельный слушатель:
`bench --site <site> telegram listen`.

Сессия (ключ авторизации и кэш сущностей) живёт в документе, а не в файле:
воркеры одноразовые, файлу негде храниться, а второй логин с того же номера
Telegram считает новым устройством.
"""

import asyncio
import json
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.utils.password import set_encrypted_password

# Сущностей (людей, групп, каналов) у активного аккаунта накапливаются тысячи.
# Без них Telethon не соберёт InputPeer и не сможет ни писать, ни читать историю,
# но и расти бесконечно кэшу незачем.
MAX_CACHED_ENTITIES = 20000

DEVICE_MODEL = "Frappe"
SYSTEM_VERSION = "habibi_telegram"

# Класс сессии собирается один раз при первом обращении: Telethon импортируется
# лениво, а наследоваться от его StringSession можно только после импорта.
_SESSION_CLASS = None


def require_telethon():
	"""Telethon либо есть, либо пользователь получает внятную инструкцию."""
	try:
		import telethon
	except ImportError:
		frappe.throw(
			_(
				"Telethon is not installed, so personal Telegram accounts are unavailable. "
				"Install it in the bench environment: ./env/bin/pip install telethon"
			),
			title=_("MTProto client is missing"),
		)

	return telethon


# -- сессия ------------------------------------------------------------------


def _session_class():
	global _SESSION_CLASS

	if _SESSION_CLASS is not None:
		return _SESSION_CLASS

	require_telethon()

	from telethon.sessions import StringSession
	from telethon.tl import types

	class FrappeSession(StringSession):
		"""
		StringSession хранит только ключ авторизации и адрес датацентра, а кэш
		сущностей и состояние апдейтов держит в памяти — то есть теряет их на
		каждом запросе. Здесь и то и другое ездит отдельным JSON-словарём,
		который зовущая сторона сохраняет в документ.
		"""

		def __init__(self, string: str = None, cache: dict = None):
			super().__init__(string or None)
			self._load_cache(cache or {})

		def _load_cache(self, cache: dict):
			for row in cache.get("entities") or []:
				# Строка кэша — это кортеж, форму которого задаёт сам Telethon;
				# мы её не разбираем, только храним
				if isinstance(row, list | tuple):
					self._entities.add(tuple(row))

			for key, state in (cache.get("update_states") or {}).items():
				try:
					self._update_states[int(key)] = types.updates.State(
						pts=state["pts"],
						qts=state["qts"],
						date=datetime.fromtimestamp(state["date"], tz=timezone.utc),
						seq=state["seq"],
						unread_count=state.get("unread_count") or 0,
					)
				except (KeyError, TypeError, ValueError):
					continue

		def dump_cache(self) -> dict:
			entities = [list(row) for row in self._entities][:MAX_CACHED_ENTITIES]

			states = {}
			for key, state in self._update_states.items():
				date = getattr(state, "date", None)
				states[str(key)] = {
					"pts": getattr(state, "pts", 0),
					"qts": getattr(state, "qts", 0),
					"date": int(date.timestamp()) if date else 0,
					"seq": getattr(state, "seq", 0),
					"unread_count": getattr(state, "unread_count", 0),
				}

			return {"entities": entities, "update_states": states}

	_SESSION_CLASS = FrappeSession

	return _SESSION_CLASS


def load_session(account):
	"""Собрать сессию Telethon из документа Telegram Account."""
	cache = {}
	if account.get("session_cache"):
		try:
			cache = json.loads(account.session_cache)
		except ValueError:
			cache = {}

	string = None
	if not account.is_new():
		string = account.get_password("session_string", raise_exception=False)

	return _session_class()(string=string, cache=cache)


def store_session(account, session):
	"""
	Сложить сессию обратно в документ.

	Ключ авторизации — это доступ к аккаунту целиком, поэтому он уезжает в
	хранилище паролей (`__Auth`), а не в колонку таблицы. Кэш сущностей —
	обычное поле: без ключа он бесполезен.
	"""
	if account.is_new():
		return

	try:
		string = session.save()
	except Exception:
		# Сессия без ключа авторизации сохраниться не может — так бывает,
		# если логин оборвался на середине
		string = ""

	set_encrypted_password(account.doctype, account.name, string or "", "session_string")
	frappe.db.set_value(
		account.doctype,
		account.name,
		{
			# В самой таблице лежит заглушка: значение живёт в __Auth
			"session_string": "*" * 10 if string else "",
			"session_cache": json.dumps(session.dump_cache(), separators=(",", ":")),
		},
		update_modified=False,
	)


def clear_session(account):
	set_encrypted_password(account.doctype, account.name, "", "session_string")
	frappe.db.set_value(
		account.doctype,
		account.name,
		{"session_string": "", "session_cache": ""},
		update_modified=False,
	)


# -- соединение --------------------------------------------------------------


def make_client(account, session, receive_updates: bool = False):
	from telethon import TelegramClient

	from habibi_telegram import __version__

	api_id = frappe.utils.cint(account.api_id)
	api_hash = account.api_hash if account.is_new() else account.get_password("api_hash")

	if not api_id or not api_hash:
		frappe.throw(_("Fill in API ID and API Hash first — get them at my.telegram.org"))

	return TelegramClient(
		session,
		api_id,
		api_hash,
		device_model=DEVICE_MODEL,
		system_version=SYSTEM_VERSION,
		app_version=__version__,
		# Апдейты нам приносит getDifference; фоновый цикл в короткоживущем
		# соединении только мешает — он успевает разобрать их до нашего запроса
		# и сдвинуть состояние. Слушателю (bench telegram listen) нужно наоборот.
		receive_updates=receive_updates,
	)


def call(account, coro_fn, save_session: bool = True, receive_updates: bool = False):
	"""
	Выполнить корутину с подключённым клиентом.

	coro_fn(client) — корутина; клиент уже соединён, но не обязательно
	авторизован (на этапе логина авторизации ещё нет).

	Клиент создаётся внутри asyncio.run: Telethon привязывается к работающему
	циклу событий, а объект сессии переживает его и уносит обновлённое
	состояние в документ.
	"""
	require_telethon()

	session = load_session(account)

	async def _run():
		client = make_client(account, session, receive_updates=receive_updates)
		await client.connect()
		try:
			return await coro_fn(client)
		finally:
			try:
				await client.disconnect()
			except Exception:
				# Соединение и так закрывается; падать на этом незачем
				frappe.clear_last_message()

	try:
		return asyncio.run(_run())
	except Exception as e:
		_handle_error(account, e)
		raise
	finally:
		if save_session:
			store_session(account, session)


def _error_class(name: str):
	"""
	Классы ошибок Telethon генерируются из схемы MTProto и от версии к версии
	слегка разъезжаются. Отсутствующее имя не должно ронять обработчик ошибок.
	"""
	from telethon import errors

	return getattr(errors, name, _Missing)


class _Missing(Exception):
	"""Заглушка для ошибки, которой в этой версии Telethon нет."""


def _handle_error(account, error):
	"""Мёртвую сессию отмечаем в документе, чтобы не гадать, почему тишина."""
	fatal = tuple(
		_error_class(name)
		for name in (
			"AuthKeyUnregisteredError",
			"AuthKeyDuplicatedError",
			"SessionRevokedError",
			"SessionExpiredError",
			"UserDeactivatedError",
		)
	)

	if isinstance(error, fatal) and not account.is_new():
		frappe.db.set_value(
			account.doctype,
			account.name,
			{"status": "Disconnected", "last_error": str(error)},
			update_modified=False,
		)


def describe_error(error) -> str:
	"""Понятный текст вместо имени класса исключения Telethon."""
	if isinstance(error, frappe.ValidationError):
		# Наш собственный frappe.throw — он уже написан по-человечески
		return str(error)

	if isinstance(error, _error_class("FloodWaitError")):
		return _("Telegram asks to wait {0} seconds before trying again").format(
			getattr(error, "seconds", "?")
		)

	messages = (
		("PhoneNumberInvalidError", _("Telegram does not know this phone number")),
		("PhoneNumberBannedError", _("This phone number is banned in Telegram")),
		("PhoneCodeInvalidError", _("Wrong code")),
		("PhoneCodeExpiredError", _("The code has expired, request a new one")),
		("PasswordHashInvalidError", _("Wrong two-factor password")),
		(
			"AuthKeyUnregisteredError",
			_("The session was revoked from Telegram settings — sign in again"),
		),
		("SessionRevokedError", _("The session was revoked from Telegram settings — sign in again")),
		("MessageNotModifiedError", _("The message text has not changed")),
		("MessageIdInvalidError", _("Telegram does not have this message anymore")),
		("MessageEditTimeExpiredError", _("Telegram no longer allows editing this message")),
		("ChatAdminRequiredError", _("Not enough rights in this chat")),
	)

	for name, text in messages:
		if isinstance(error, _error_class(name)):
			return text

	return str(error)


# -- разбор объектов MTProto -------------------------------------------------


def peer_id(peer) -> int | None:
	"""
	Идентификатор в той же разметке, что и в Bot API: люди положительные,
	группы отрицательные, каналы с приставкой -100. Так один и тот же чат
	получает одинаковый chat_id и от бота, и от аккаунта.
	"""
	from telethon import utils

	try:
		return utils.get_peer_id(peer)
	except (TypeError, ValueError):
		return None


def entity_to_chat(entity) -> dict | None:
	"""Сущность Telethon → словарь чата в форме Bot API (её ждёт telegram_chat)."""
	from telethon.tl import types

	identifier = peer_id(entity)
	if identifier is None:
		return None

	if isinstance(entity, types.User):
		return {
			"id": identifier,
			"type": "private",
			"username": entity.username,
			"first_name": entity.first_name,
			"last_name": entity.last_name,
		}

	if isinstance(entity, types.Chat | types.ChatForbidden):
		return {"id": identifier, "type": "group", "title": entity.title}

	if isinstance(entity, types.Channel | types.ChannelForbidden):
		return {
			"id": identifier,
			"type": "supergroup" if getattr(entity, "megagroup", False) else "channel",
			"title": entity.title,
			"username": getattr(entity, "username", None),
		}

	return None


def entity_to_user(entity) -> dict | None:
	"""Сущность Telethon → словарь пользователя в форме Bot API."""
	from telethon.tl import types

	if not isinstance(entity, types.User):
		return None

	return {
		"id": entity.id,
		"username": entity.username,
		"first_name": entity.first_name,
		"last_name": entity.last_name,
	}


def index_entities(users: list = None, chats: list = None) -> dict:
	"""
	Собрать все сущности ответа в один индекс по chat_id.

	getDifference отдаёт сообщения отдельно от людей и чатов, к которым они
	относятся, — сопоставляем сами.
	"""
	index = {}

	for entity in list(users or []) + list(chats or []):
		identifier = peer_id(entity)
		if identifier is not None:
			index[identifier] = entity

	return index


def to_system_datetime(value):
	"""
	Дата MTProto → дата в таймзоне сайта, как любит frappe.

	Telethon отдаёт datetime с таймзоной UTC, а frappe.utils сам навешивает
	UTC и на уже размеченной дате падает — снимаем tzinfo заранее.
	"""
	if not value:
		return None

	if getattr(value, "tzinfo", None) is not None:
		value = value.astimezone(timezone.utc).replace(tzinfo=None)

	return frappe.utils.convert_utc_to_system_timezone(value).replace(tzinfo=None)


def parse_mode_for_telethon(parse_mode: str = None):
	"""
	Bot API и Telethon называют разметку по-разному.

	MarkdownV2 у Telethon отдельного режима не имеет: его 'md' — это тот же
	современный markdown, разница только в наборе экранируемых символов.
	"""
	if not parse_mode:
		return None

	value = parse_mode.strip().lower()
	if value == "html":
		return "html"

	if value in ("markdown", "markdownv2", "md"):
		return "md"

	frappe.throw(_("Invalid parse mode '{0}'. Use HTML, MarkdownV2 or leave it empty.").format(parse_mode))
