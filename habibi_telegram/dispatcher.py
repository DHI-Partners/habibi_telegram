"""
Маршрутизация входящих апдейтов.

Заменяет Dispatcher из python-telegram-bot. Обработчики регистрируются через
хук `telegram_bot_handler`: каждый пункт — функция `setup(registry, telegram_bot)`,
которая навешивает себя на реестр.

	def setup(registry, telegram_bot):
		registry.command("help", show_help)

Обработчики одной группы выполняются в порядке регистрации, группы — по
возрастанию номера (отрицательные раньше). Чтобы прервать цепочку, обработчик
кидает StopHandling — это аналог DispatcherHandlerStop.
"""

import frappe

from habibi_telegram.utils import update as u


class StopHandling(Exception):
	"""Прервать обработку апдейта: дальше по цепочке никто не вызывается."""

	def __init__(self, state: dict = None):
		super().__init__()
		self.state = state


class Registry:
	"""Собирает обработчики и отдаёт их в порядке выполнения."""

	def __init__(self):
		self._handlers = []
		self._seq = 0

	def add(self, matcher, fn, group: int = 0):
		self._seq += 1
		self._handlers.append((group, self._seq, matcher, fn))

	def command(self, name: str, fn, group: int = 0):
		"""Реакция на /name."""
		self.add(lambda update: u.command(update) == name.lower(), fn, group)

	def message(self, fn, group: int = 0, predicate=None):
		"""Любое текстовое сообщение (или то, что подойдёт под predicate)."""
		self.add(
			lambda update: bool(u.effective_message(update))
			and not update.get("callback_query")
			and (predicate(update) if predicate else True),
			fn,
			group,
		)

	def callback_query(self, fn, data: str = None, prefix: str = None, group: int = 0):
		"""Нажатие на inline-кнопку."""

		def matcher(update):
			value = u.callback_data(update)
			if value is None:
				return False
			if data is not None:
				return value == data
			if prefix is not None:
				return value.startswith(prefix)
			return True

		self.add(matcher, fn, group)

	def any(self, fn, group: int = 0):
		self.add(lambda update: True, fn, group)

	def handlers(self):
		return [(m, fn) for _, _, m, fn in sorted(self._handlers, key=lambda x: (x[0], x[1]))]


class Context:
	"""
	Всё, что обработчику нужно знать об апдейте.

	Заменяет CallbackContext. Поля telegram_user / telegram_chat / telegram_message
	заполняет pre-processor из handlers.logging.
	"""

	def __init__(self, telegram_bot, update: dict):
		self.telegram_bot = telegram_bot
		self.update = update
		self.telegram_user = None
		self.telegram_chat = None
		self.telegram_message = None

		self._api = None

	@property
	def bot(self):
		if self._api is None:
			from habibi_telegram.client import get_bot

			self._api = get_bot(self.telegram_bot.name)

		return self._api

	@property
	def chat_id(self):
		chat = u.effective_chat(self.update)

		return chat.get("id") if chat else None

	@property
	def text(self):
		return u.message_text(self.update)

	def reply(self, text: str, parse_mode: str = None, reply_markup=None):
		"""Ответить в тот же чат и записать сообщение в историю."""
		from habibi_telegram.client import send_message

		return send_message(
			text,
			parse_mode=parse_mode,
			from_bot=self.telegram_bot.name,
			chat_id=self.chat_id,
			reply_markup=reply_markup,
		)

	def answer_callback(self, text: str = None, show_alert: bool = False):
		query = u.callback_query(self.update)
		if not query:
			return

		self.bot.answer_callback_query(query["id"], text=text, show_alert=show_alert)

	# -- состояние диалога --------------------------------------------------

	def get_state(self) -> dict:
		return self.telegram_user.get_conversation_state() if self.telegram_user else {}

	def set_state(self, state: dict):
		if self.telegram_user:
			self.telegram_user.set_conversation_state(state)

	def clear_state(self):
		if self.telegram_user:
			self.telegram_user.clear_conversation_state()


def process_update(telegram_bot: str, update: dict):
	"""Точка входа: вызывается из webhook'а на каждый апдейт."""
	bot_doc = frappe.get_doc("Telegram Bot", telegram_bot)
	context = Context(telegram_bot=bot_doc, update=update)

	# Обработчики переключают пользователя сессии под того, кто пишет боту;
	# исходного возвращаем на место, чтобы не тащить чужие права дальше по запросу
	original_user = frappe.session.user
	frappe.flags.in_telegram_update = True
	try:
		_run_hooks("telegram_update_pre_processors", context)

		registry = Registry()
		for method in frappe.get_hooks("telegram_bot_handler") or []:
			frappe.get_attr(method)(registry=registry, telegram_bot=bot_doc)

		for matcher, handler in registry.handlers():
			if not matcher(update):
				continue
			handler(context)

		_run_hooks("telegram_update_post_processors", context)
	except StopHandling:
		pass
	finally:
		frappe.flags.in_telegram_update = False
		frappe.set_user(original_user)

	return True


def _run_hooks(hook: str, context: Context):
	for method in frappe.get_hooks(hook) or []:
		frappe.get_attr(method)(context)
