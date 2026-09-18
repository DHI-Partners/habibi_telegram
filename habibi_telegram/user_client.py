"""
Публичный API личного аккаунта: вход, синхронизация, отправка/правка/удаление.

	from habibi_telegram.user_client import send_message
	send_message("Иван", chat_id=-1001234567890, text="Выехал")

Всё, что здесь есть, работает поверх MTProto (см. habibi_telegram.mtproto) и
требует подключённого документа Telegram Account.

Апдейты забираются запросом updates.getDifference по сохранённому состоянию:
Telegram помнит очередь изменений на несколько дней, поэтому короткое соединение
раз в минуту не теряет ни сообщений, ни правок, ни удалений. Состояние (pts/qts/
date) лежит в поле sync_state — по нему же видно, докуда мы дочитали.
"""

import io
import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from habibi_telegram import mtproto
from habibi_telegram.handlers import account as store

# Сколько диалогов и сколько сообщений в каждом подтягиваем при первом запуске
# и после разрыва состояния
DEFAULT_DIALOG_LIMIT = 50
DEFAULT_HISTORY_LIMIT = 20

# Дольше этого синхронизация одного аккаунта идти не должна — если идёт, значит
# задача повисла, и блокировку пора отпускать
SYNC_LOCK_TIMEOUT = 600


def get_account(account) -> "frappe.Document":
	"""account — имя документа или сам документ."""
	if isinstance(account, str):
		return frappe.get_doc("Telegram Account", account)

	return account


# -- вход --------------------------------------------------------------------


def request_code(account) -> dict:
	"""Запросить код подтверждения. Telegram пришлёт его в приложение или SMS."""
	account = get_account(account)
	mtproto.require_telethon()

	if not account.phone:
		frappe.throw(_("Fill in the phone number of the Telegram account"))

	async def _op(client):
		sent = await client.send_code_request(account.phone)

		return sent.phone_code_hash

	try:
		phone_code_hash = mtproto.call(account, _op)
	except Exception as e:
		frappe.throw(mtproto.describe_error(e), title=_("Telegram refused the request"))

	account.db_set(
		{
			"phone_code_hash": phone_code_hash,
			"code_requested_on": now_datetime(),
			"status": "Code Sent",
			"last_error": "",
		},
		update_modified=False,
	)

	return {"status": "Code Sent"}


def sign_in(account, code: str = None, password: str = None) -> dict:
	"""
	Завершить вход: сначала кодом, при включённой двухфакторке — ещё и паролем.

	Возвращает {"password_required": True}, если Telegram ждёт пароль; тогда
	этот же метод зовут второй раз, уже с password.
	"""
	account = get_account(account)
	mtproto.require_telethon()

	from telethon import errors

	if not code and not password:
		frappe.throw(_("Enter the code Telegram has sent you"))

	phone = account.phone
	phone_code_hash = account.phone_code_hash

	async def _op(client):
		if password:
			return await client.sign_in(password=password)

		return await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)

	try:
		me = mtproto.call(account, _op)
	except errors.SessionPasswordNeededError:
		account.db_set("status", "Password Required", update_modified=False)

		return {"password_required": True}
	except Exception as e:
		account.db_set("last_error", str(e), update_modified=False)
		frappe.throw(mtproto.describe_error(e), title=_("Sign in failed"))

	_save_identity(account, me)

	# Первый разбор истории может занять минуты — не держим на нём форму
	frappe.enqueue(
		"habibi_telegram.user_client.sync_account",
		queue="long",
		account=account.name,
		job_id=f"telegram-sync-{account.name}",
		enqueue_after_commit=True,
	)

	return {"status": "Connected", "username": account.username}


def log_out(account, forget_session: bool = True) -> dict:
	"""
	Отключить аккаунт: сессия на стороне Telegram завершается, ключ стирается.

	forget_session=False оставляет ключ на месте — так отключают синхронизацию,
	не разлогиниваясь.
	"""
	account = get_account(account)

	if forget_session:
		mtproto.require_telethon()

		async def _op(client):
			if await client.is_user_authorized():
				await client.log_out()

			return True

		try:
			# save_session=False: сохранять уже нечего, ключ отозван
			mtproto.call(account, _op, save_session=False)
		except Exception as e:
			# Сессию могли отозвать из приложения раньше нас — это не ошибка
			frappe.log_error(title=f"Telegram log out ({account.name})", message=str(e))

		mtproto.clear_session(account)

	account.db_set(
		{"status": "Disconnected", "phone_code_hash": "", "sync_state": ""},
		update_modified=False,
	)

	return {"status": "Disconnected"}


def _save_identity(account, me):
	"""Записать, кем мы в итоге вошли."""
	full_name = " ".join(x for x in (me.first_name, me.last_name) if x).strip()

	account.db_set(
		{
			"account_id": str(me.id),
			"username": ("@" + me.username) if me.username else "",
			"full_name": full_name or str(me.id),
			"status": "Connected",
			"phone_code_hash": "",
			"last_error": "",
		},
		update_modified=False,
	)


# -- синхронизация -----------------------------------------------------------


def sync_all_accounts():
	"""Планировщик: раз в минуту разложить синхронизацию по фоновым задачам."""
	from habibi_telegram import listener

	accounts = frappe.get_all(
		"Telegram Account",
		filters={"enabled": 1, "sync_enabled": 1, "status": "Connected"},
		pluck="name",
	)

	for name in accounts:
		# Аккаунт держит слушатель — второе соединение той же сессией
		# Telegram может счесть угоном и разорвать обе
		if listener.is_alive(name):
			continue

		frappe.enqueue(
			"habibi_telegram.user_client.sync_account",
			queue="long",
			account=name,
			job_id=f"telegram-sync-{name}",
			deduplicate=True,
		)


def sync_account(account, history_limit: int = None) -> dict:
	"""
	Забрать всё, что накопилось с прошлого раза.

	Возвращает счётчики: сколько сообщений добавилось, изменилось и удалилось.
	"""
	account = get_account(account)
	mtproto.require_telethon()

	lock = _sync_lock(account.name)
	if not lock.acquire(blocking=False):
		# Предыдущий проход ещё идёт: параллельно дёргать getDifference нельзя,
		# состояние сдвинется дважды и часть апдейтов потеряется
		return {"skipped": True}

	stats = {"new": 0, "edited": 0, "deleted": 0}

	try:
		mtproto.call(account, lambda client: _sync(client, account, stats, history_limit))
	except Exception as e:
		# Фоновая задача откатит транзакцию — записываем причину отдельным
		# коммитом, иначе на форме останется пустое поле и загадка
		frappe.db.rollback()
		account.db_set("last_error", str(e), update_modified=False)
		frappe.db.commit()
		raise
	finally:
		try:
			lock.release()
		except Exception:
			frappe.clear_last_message()

	account.db_set({"last_sync_on": now_datetime(), "last_error": ""}, update_modified=False)
	frappe.db.commit()

	return stats


async def _sync(client, account, stats: dict, history_limit: int = None):
	from telethon.tl import functions, types

	if not await client.is_user_authorized():
		account.db_set(
			{"status": "Disconnected", "last_error": "Session is not authorized"},
			update_modified=False,
		)
		frappe.throw(_("Telegram account {0} is not signed in").format(account.name))

	state = _get_sync_state(account)

	if not state.get("pts"):
		# Первый заход: показать хотя бы недавнюю переписку, иначе раздел
		# пустует до первого входящего сообщения
		await _fetch_recent(client, account, stats, history_limit=history_limit)
		await _remember_state(client, account)

		return

	pts, qts, date = state["pts"], state.get("qts") or 0, state.get("date") or 0

	# Разница приходит порциями: пока в ответе DifferenceSlice, идём дальше
	# от промежуточного состояния
	for _iteration in range(50):
		difference = await client(
			functions.updates.GetDifferenceRequest(pts=pts, date=date, qts=qts)
		)

		if isinstance(difference, types.updates.DifferenceEmpty):
			_set_sync_state(account, {"pts": pts, "qts": qts, "date": difference.date, "seq": difference.seq})
			return

		if isinstance(difference, types.updates.DifferenceTooLong):
			# Состояние протухло — Telegram больше не помнит, что было. Берём
			# новую точку отсчёта и добираем недавнюю историю руками
			await _fetch_recent(client, account, stats, history_limit=history_limit)
			await _remember_state(client, account)
			return

		entities = mtproto.index_entities(difference.users, difference.chats)

		for message in difference.new_messages:
			if store.log_message(account, message, entities)[1]:
				stats["new"] += 1

		for update in difference.other_updates:
			await _apply_update(client, account, update, entities, stats)

		# Разобранное фиксируем сразу: если следующая порция упадёт, к ней же
		# и вернёмся, а прочитанное останется прочитанным
		frappe.db.commit()

		if isinstance(difference, types.updates.Difference):
			final = difference.state
			_set_sync_state(
				account,
				{"pts": final.pts, "qts": final.qts, "date": final.date, "seq": final.seq},
			)
			return

		intermediate = difference.intermediate_state
		pts, qts, date = intermediate.pts, intermediate.qts, intermediate.date
		_set_sync_state(
			account,
			{"pts": pts, "qts": qts, "date": date, "seq": intermediate.seq},
		)

	frappe.log_error(
		title=f"Telegram sync did not converge ({account.name})",
		message="getDifference keeps returning slices; state left at pts={0}".format(pts),
	)


async def _apply_update(client, account, update, entities: dict, stats: dict):
	"""Разобрать один апдейт из other_updates."""
	from telethon.tl import types

	if isinstance(update, types.UpdateNewMessage | types.UpdateNewChannelMessage):
		if store.log_message(account, update.message, entities)[1]:
			stats["new"] += 1

	elif isinstance(update, types.UpdateEditMessage | types.UpdateEditChannelMessage):
		if store.mark_edited(account, update.message, entities):
			stats["edited"] += 1

	elif isinstance(update, types.UpdateDeleteMessages):
		stats["deleted"] += store.mark_deleted(account, update.messages)

	elif isinstance(update, types.UpdateDeleteChannelMessages):
		chat_id = mtproto.peer_id(types.PeerChannel(update.channel_id))
		stats["deleted"] += store.mark_deleted(account, update.messages, chat_id=chat_id)

	elif isinstance(update, types.UpdateChannelTooLong):
		# Для канала своя очередь апдейтов, и она разошлась с нашей. Разбирать
		# её отдельным getChannelDifference — много кода ради того же результата:
		# проще перечитать последние сообщения, дубли отсекутся по message_id
		await _fetch_channel_history(client, account, update.channel_id, stats)


async def _fetch_recent(client, account, stats: dict, history_limit: int = None):
	"""Пройтись по диалогам и записать последние сообщения каждого."""
	dialog_limit = cint(account.dialog_limit) or DEFAULT_DIALOG_LIMIT
	history_limit = cint(history_limit or account.history_limit) or DEFAULT_HISTORY_LIMIT

	dialogs = await client.get_dialogs(limit=dialog_limit)

	for dialog in dialogs:
		try:
			messages = await client.get_messages(dialog.entity, limit=history_limit)
		except Exception as e:
			frappe.log_error(
				title=f"Telegram history failed ({account.name})",
				message=f"{dialog.name}: {e}",
			)
			continue

		entities = _entities_from(messages, dialog.entity)

		# Telegram отдаёт историю от новых к старым — пишем в обратном порядке,
		# чтобы «последнее сообщение» в чате осталось последним.
		# notify=False: разбор истории — не повод оповещать о сотнях сообщений
		for message in reversed(messages):
			if store.log_message(account, message, entities, notify=False)[1]:
				stats["new"] += 1

		# Разбор сотни диалогов идёт минутами; терять его из-за одного сбоя жалко
		frappe.db.commit()


async def _fetch_channel_history(client, account, channel_id, stats: dict, limit: int = 50):
	from telethon.tl import types

	try:
		entity = await client.get_entity(types.PeerChannel(channel_id))
		messages = await client.get_messages(entity, limit=limit)
	except Exception as e:
		frappe.log_error(
			title=f"Telegram channel history failed ({account.name})",
			message=f"{channel_id}: {e}",
		)
		return

	entities = _entities_from(messages, entity)

	for message in reversed(messages):
		if store.log_message(account, message, entities, notify=False)[1]:
			stats["new"] += 1


def _entities_from(messages, *extra) -> dict:
	"""
	Индекс сущностей для сообщений, полученных через клиента.

	Telethon уже разложил по каждому сообщению его отправителя и чат — остаётся
	собрать их в тот же индекс, что строится для getDifference.
	"""
	index = {}

	for entity in extra:
		identifier = mtproto.peer_id(entity)
		if identifier is not None:
			index[identifier] = entity

	for message in messages:
		for entity in (getattr(message, "sender", None), getattr(message, "chat", None)):
			if entity is None:
				continue
			identifier = mtproto.peer_id(entity)
			if identifier is not None:
				index[identifier] = entity

	return index


async def _remember_state(client, account):
	"""Запомнить текущую точку очереди апдейтов — от неё пойдёт следующий заход."""
	from telethon.tl import functions

	state = await client(functions.updates.GetStateRequest())
	_set_sync_state(
		account,
		{"pts": state.pts, "qts": state.qts, "date": state.date, "seq": state.seq},
	)


def _get_sync_state(account) -> dict:
	if not account.sync_state:
		return {}

	try:
		return json.loads(account.sync_state)
	except ValueError:
		return {}


def _set_sync_state(account, state: dict):
	date = state.get("date")
	if hasattr(date, "timestamp"):
		state = dict(state, date=int(date.timestamp()))

	account.sync_state = json.dumps(state)
	frappe.db.set_value(
		"Telegram Account", account.name, "sync_state", account.sync_state, update_modified=False
	)


class _NoLock:
	"""Запасной вариант, если у кэша не оказалось блокировок."""

	def acquire(self, blocking: bool = False) -> bool:
		return True

	def release(self):
		pass


def _sync_lock(name: str):
	"""
	Блокировка на аккаунт: планировщик и кнопка «Синхронизировать» легко
	встречаются в одну минуту, а getDifference такого не прощает — состояние
	сдвинется дважды, и часть апдейтов не увидит никто.
	"""
	try:
		return frappe.cache().lock(
			f"{frappe.local.site}:telegram-account-sync:{name}", timeout=SYNC_LOCK_TIMEOUT
		)
	except AttributeError:
		return _NoLock()


# -- отправка, правка, удаление ----------------------------------------------


def send_message(
	account,
	chat_id,
	text: str,
	parse_mode: str = None,
	reply_to=None,
	file=None,
	filename: str = None,
	automated: bool = False,
) -> str | None:
	"""
	Написать от имени аккаунта. Возвращает имя записанного Telegram Message.

	file — путь, bytes или file_id; тогда text уходит подписью.
	automated — отправил не человек (ИИ, уведомление): пометка в истории.
	"""
	account = get_account(account)
	mtproto.require_telethon()

	if not text and not file:
		frappe.throw(_("Cannot send an empty Telegram message"))

	mode = mtproto.parse_mode_for_telethon(parse_mode)

	async def _op(client):
		from telethon.tl import types

		entity = await _resolve(client, chat_id)

		if file is not None:
			# Имя файла в MTProto — это атрибут документа, отдельного параметра нет
			attributes = [types.DocumentAttributeFilename(filename)] if filename else None

			return await client.send_file(
				entity,
				file,
				caption=text or None,
				parse_mode=mode,
				reply_to=cint(reply_to) or None,
				attributes=attributes,
				force_document=bool(filename),
			)

		return await client.send_message(
			entity, text, parse_mode=mode, reply_to=cint(reply_to) or None
		)

	try:
		message = mtproto.call(account, _op)
	except Exception as e:
		frappe.throw(mtproto.describe_error(e), title=_("Telegram did not accept the message"))

	return store.log_message(account, message, _entities_from([message]), automated=automated)[0]


def send_voice(
	account,
	chat_id,
	content: bytes,
	caption: str = None,
	parse_mode: str = None,
	duration: int = None,
	reply_to=None,
) -> str | None:
	"""
	Голосовое от имени аккаунта. Возвращает имя записанного Telegram Message.

	Запись приводится к тому, что Telegram принимает голосовым, — иначе она
	приедет в чат обычным файлом (см. habibi_telegram.utils.audio).
	"""
	from habibi_telegram.utils.audio import prepare_voice

	account = get_account(account)
	mtproto.require_telethon()

	voice = prepare_voice(content)
	mode = mtproto.parse_mode_for_telethon(parse_mode)

	async def _op(client):
		from telethon.tl import types

		entity = await _resolve(client, chat_id)

		stream = io.BytesIO(voice.content)
		# Telethon берёт имя файла у потока; без него запись уедет «unnamed»
		stream.name = voice.filename

		return await client.send_file(
			entity,
			stream,
			caption=caption or None,
			parse_mode=mode,
			reply_to=cint(reply_to) or None,
			mime_type=voice.mime,
			voice_note=True,
			# Свои атрибуты Telethon ставит выше вычисленных: длительность он
			# сам не измерит, а без неё Telegram рисует пустую волну
			attributes=[
				types.DocumentAttributeAudio(
					duration=cint(duration or voice.duration), voice=True
				)
			],
		)

	try:
		message = mtproto.call(account, _op)
	except Exception as e:
		frappe.throw(mtproto.describe_error(e), title=_("Telegram did not accept the message"))

	return store.log_message(account, message, _entities_from([message]))[0]


def download_media(account, chat_id, message_id, max_bytes: int = None) -> frappe._dict:
	"""
	Забрать вложение сообщения: голосовое, фотографию, документ.

	Ссылок на файлы мы не храним — да они в MTProto и не живут отдельно от
	сообщения. Поэтому сообщение сначала перезапрашивается по своему номеру, а
	вложение качается уже из него.

	Возвращает content / filename / mime.
	"""
	account = get_account(account)
	mtproto.require_telethon()

	async def _op(client):
		entity = await _resolve(client, chat_id)

		messages = await client.get_messages(entity, ids=[cint(message_id)])
		message = messages[0] if messages else None

		if not message or not getattr(message, "media", None):
			return None

		media = message.file
		size = getattr(media, "size", None) or 0

		if max_bytes and size > max_bytes:
			return frappe._dict(too_large=size)

		content = await client.download_media(message, file=bytes)

		return frappe._dict(
			content=content,
			filename=getattr(media, "name", None)
			or f"{message_id}{getattr(media, 'ext', '') or ''}",
			mime=getattr(media, "mime_type", None) or "application/octet-stream",
		)

	try:
		media = mtproto.call(account, _op)
	except Exception as e:
		frappe.throw(mtproto.describe_error(e), title=_("Could not download the attachment"))

	if not media:
		frappe.throw(_("The message no longer exists in Telegram or has no attachment"))

	if media.get("too_large"):
		frappe.throw(
			_("Attachment is too large: {0} MB").format(round(media.too_large / 1024 / 1024, 1))
		)

	return media


def edit_message(account, chat_id, message_id, text: str, parse_mode: str = None) -> str | None:
	"""
	Исправить своё сообщение.

	Telegram разрешает править только собственные сообщения и только 48 часов
	(в своих каналах — с правами администратора).
	"""
	account = get_account(account)
	mtproto.require_telethon()

	if not text:
		frappe.throw(_("Cannot save an empty message. Delete it instead."))

	mode = mtproto.parse_mode_for_telethon(parse_mode)

	async def _op(client):
		entity = await _resolve(client, chat_id)

		return await client.edit_message(entity, cint(message_id), text, parse_mode=mode)

	try:
		message = mtproto.call(account, _op)
	except Exception as e:
		frappe.throw(mtproto.describe_error(e), title=_("Telegram did not accept the edit"))

	return store.mark_edited(account, message, _entities_from([message]))


def delete_message(account, chat_id, message_id, revoke: bool = True) -> int:
	"""
	Удалить сообщение. revoke=True — у всех участников, иначе только у себя.
	"""
	account = get_account(account)
	mtproto.require_telethon()

	async def _op(client):
		entity = await _resolve(client, chat_id)

		return await client.delete_messages(entity, [cint(message_id)], revoke=bool(revoke))

	try:
		mtproto.call(account, _op)
	except Exception as e:
		frappe.throw(mtproto.describe_error(e), title=_("Telegram did not accept the deletion"))

	return store.mark_deleted(account, [message_id], chat_id=chat_id)


def fetch_dialogs(account, limit: int = None) -> list:
	"""Перечитать список диалогов: чаты заводятся, названия обновляются."""
	account = get_account(account)
	mtproto.require_telethon()

	limit = cint(limit) or cint(account.dialog_limit) or DEFAULT_DIALOG_LIMIT

	async def _op(client):
		result = []
		for dialog in await client.get_dialogs(limit=limit):
			chat = mtproto.entity_to_chat(dialog.entity)
			if chat:
				result.append(chat)

		return result

	try:
		chats = mtproto.call(account, _op)
	except Exception as e:
		frappe.throw(mtproto.describe_error(e), title=_("Could not read the dialog list"))

	from habibi_telegram.habibi_telegram.doctype.telegram_chat import telegram_chat as chat_store

	for chat in chats:
		chat_store.get_or_create(chat, telegram_account=account.name)

	return chats


async def _resolve(client, chat_id):
	"""
	Найти собеседника по chat_id.

	Идентификатора мало: MTProto хочет ещё access_hash, и берётся он из кэша
	сущностей в сессии. Если чат в кэш не попал (аккаунт подключили только что
	или диалог давно не всплывал) — подсказываем, что делать.
	"""
	try:
		return await client.get_entity(cint(chat_id))
	except (ValueError, TypeError):
		frappe.throw(
			_(
				"This account does not know chat {0} yet. Press 'Sync Now' on the account "
				"so Telegram sends its dialog list, then try again."
			).format(chat_id)
		)


# -- realtime ----------------------------------------------------------------


def listen(account, forever: bool = True, seconds: int = None, heartbeat: bool = False):
	"""
	Слушать апдейты в открытом соединении — для `bench telegram listen`.

	Обычной установке это не нужно: getDifference раз в минуту забирает то же
	самое. Но если задержка в минуту неприемлема, процесс можно повесить в
	supervisor и получать сообщения мгновенно.

	heartbeat — для listen-all: отмечаться в redis, чтобы cron не открывал
	второе соединение, и отключиться самому, когда аккаунт выключат на форме.
	"""
	account = get_account(account)
	mtproto.require_telethon()

	from telethon import events

	stats = {"new": 0, "edited": 0, "deleted": 0}

	# Пропущенное за время простоя добираем до подключения: события приходят
	# только с момента, когда соединение уже открыто, а asyncio.run внутри
	# работающего цикла не запустишь
	try:
		sync_account(account.name)
	except Exception:
		frappe.log_error(title="Telegram catch-up failed", message=frappe.get_traceback())

	async def _op(client):
		if not await client.is_user_authorized():
			frappe.throw(_("Telegram account {0} is not signed in").format(account.name))

		@client.on(events.NewMessage)
		async def _on_new(event):
			_in_transaction(store.log_message, account, event.message, _event_entities(event))
			stats["new"] += 1

		@client.on(events.MessageEdited)
		async def _on_edit(event):
			_in_transaction(store.mark_edited, account, event.message, _event_entities(event))
			stats["edited"] += 1

		@client.on(events.MessageDeleted)
		async def _on_delete(event):
			_in_transaction(store.mark_deleted, account, event.deleted_ids, event.chat_id)
			stats["deleted"] += 1

		if heartbeat:
			import asyncio

			from habibi_telegram import listener

			async def _beat():
				while True:
					listener.mark_alive(account.name)
					await asyncio.sleep(listener.HEARTBEAT_EVERY)
					if not listener.should_listen(account.name):
						await client.disconnect()
						return

			asyncio.get_running_loop().create_task(_beat())

		if forever:
			await client.run_until_disconnected()
		else:
			import asyncio

			await asyncio.sleep(cint(seconds) or 60)

	mtproto.call(account, _op, receive_updates=True)

	return stats


def _event_entities(event) -> dict:
	index = {}

	for entity in (getattr(event, "_chat", None), getattr(event, "_sender", None)):
		identifier = mtproto.peer_id(entity) if entity is not None else None
		if identifier is not None:
			index[identifier] = entity

	return index


def _in_transaction(fn, *args):
	"""
	Обработчик события живёт вне запроса: коммит и откат на нём самом.

	Ошибка одного сообщения не должна ронять слушателя — иначе процесс умрёт
	на первом же кривом апдейте и перестанет получать всё остальное.
	"""
	try:
		result = fn(*args)
		frappe.db.commit()

		return result
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Telegram listener failed", message=frappe.get_traceback())
		frappe.db.commit()
