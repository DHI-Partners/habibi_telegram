"""
Разбор объекта Update из Bot API.

python-telegram-bot оборачивал апдейт в объекты с атрибутами; здесь апдейт —
обычный dict, каким его прислал Telegram, а эти функции достают из него то,
что нужно чаще всего.

https://core.telegram.org/bots/api#update
"""

# Ключи, в которых может лежать сообщение, в порядке приоритета
_MESSAGE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post")


def effective_message(update: dict) -> dict | None:
	"""Сообщение апдейта. Для нажатия на кнопку — сообщение, под которым кнопка."""
	for key in _MESSAGE_KEYS:
		if update.get(key):
			return update[key]

	callback_query = update.get("callback_query")
	if callback_query:
		return callback_query.get("message")

	return None


def effective_user(update: dict) -> dict | None:
	"""Кто прислал апдейт. https://core.telegram.org/bots/api#user"""
	for key in (*_MESSAGE_KEYS, "callback_query"):
		if update.get(key):
			return update[key].get("from")

	return None


def effective_chat(update: dict) -> dict | None:
	message = effective_message(update)

	return message.get("chat") if message else None


def message_text(update: dict) -> str | None:
	message = effective_message(update)
	if not message:
		return None

	return message.get("text") or message.get("caption")


def callback_query(update: dict) -> dict | None:
	return update.get("callback_query")


def callback_data(update: dict) -> str | None:
	query = update.get("callback_query")

	return query.get("data") if query else None


def command(update: dict) -> str | None:
	"""
	Команда без слэша и без @упоминания бота: "/start@my_bot ru" -> "start".

	Telegram помечает команды сущностью bot_command в самом начале текста;
	проверяем именно её, чтобы не считать командой текст вида "5/6".
	"""
	message = effective_message(update)
	if not message or update.get("callback_query"):
		return None

	text = message.get("text") or ""
	entities = message.get("entities") or []

	is_command = any(
		e.get("type") == "bot_command" and e.get("offset") == 0 for e in entities
	)
	if not is_command:
		return None

	token = text.split(maxsplit=1)[0]

	return token.lstrip("/").split("@", 1)[0].lower()


def command_args(update: dict) -> list[str]:
	"""Аргументы после команды: "/lang ru RU" -> ["ru", "RU"]."""
	if not command(update):
		return []

	text = (effective_message(update) or {}).get("text") or ""
	parts = text.split()

	return parts[1:]
