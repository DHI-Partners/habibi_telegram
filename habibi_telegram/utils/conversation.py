"""
Пошаговый сбор данных в диалоге.

Аналог ConversationHandler из python-telegram-bot, но состояние лежит не в
памяти процесса, а в поле Telegram User: на вебхуках каждый апдейт приходит
отдельным HTTP-запросом, и в памяти между ними ничего не переживает.
"""

import re

import frappe
from frappe import _

# Типы, которые понимает meta
STRING_TYPES = ("str", "string")
INT_TYPES = ("int", "integer")
FLOAT_TYPES = ("flt", "float")


def collect_conversation_details(key: str, meta: list, context) -> frappe._dict:
	"""
	Собрать набор значений, задавая по одному вопросу за апдейт.

	meta — список описаний:
		{"key": "email", "label": "Email", "type": "regex", "options": r"^.+@.+\\..+$"}
		{"key": "pwd", "label": "Password", "type": "password"}
		{"key": "gender", "label": "Gender", "type": "select", "options": "Male\\nFemale"}

	Поддерживаемые type: str/string, password, int/integer, flt/float, select, regex.
	Значения обязательны, если явно не указано reqd=False.

	Возвращает собранное; готовность проверяется по ключу _is_complete.
	"""
	state = context.get_state()
	order = [m["key"] for m in meta]
	meta_by_key = {m["key"]: m for m in meta}
	next_of = {k: (order[i + 1] if i + 1 < len(order) else None) for i, k in enumerate(order)}

	details = state.get(key)
	if details is None:
		details = {
			"_is_complete": False,
			"_last_detail_asked": None,
			"_next_detail_to_ask": order[0],
		}
		state[key] = details

	if details.get("_is_complete"):
		return frappe._dict(details)

	# Разбираем ответ на предыдущий вопрос
	if details.get("_last_detail_asked"):
		detail_meta = meta_by_key[details["_last_detail_asked"]]
		validated, value, error = _validate(detail_meta, context)

		if not validated:
			context.reply(error)
			context.reply(_("Please try again"))
			context.set_state(state)
			return frappe._dict(details)

		details[detail_meta["key"]] = value
		details["_next_detail_to_ask"] = next_of[detail_meta["key"]]
		if not details["_next_detail_to_ask"]:
			details["_is_complete"] = True

	# Задаём следующий вопрос
	if details.get("_next_detail_to_ask"):
		detail_meta = meta_by_key[details["_next_detail_to_ask"]]
		prompt = detail_meta.get("prompt") or _("Please provide your {0}").format(
			detail_meta.get("label") or detail_meta["key"]
		)
		context.reply(prompt, reply_markup=_reply_markup(detail_meta))

		details["_last_detail_asked"] = detail_meta["key"]
		details["_next_detail_to_ask"] = None

	collected = frappe._dict(details)

	if details.get("_is_complete"):
		# Дальше значения несёт вызывающая сторона, в состоянии им делать нечего
		state.pop(key, None)

	context.set_state(state)

	return collected


def reset(key: str, context):
	"""Забыть частично собранные значения и начать заново."""
	state = context.get_state()
	state.pop(key, None)
	context.set_state(state)


def _reply_markup(detail_meta: dict):
	if detail_meta.get("type") != "select":
		return None

	options = (detail_meta.get("options") or "").split("\n")

	return {
		"keyboard": [[{"text": option} for option in options if option]],
		"one_time_keyboard": True,
		"resize_keyboard": True,
	}


def _validate(detail_meta: dict, context):
	"""Возвращает (validated, value, error_message)."""
	text = context.text
	field_type = detail_meta.get("type")
	label = detail_meta.get("label") or detail_meta["key"]
	required = detail_meta.get("reqd", True)

	if field_type == "select":
		options = [o for o in (detail_meta.get("options") or "").split("\n") if o]
		if text not in options:
			return False, None, _("Please select from the given options")
		return True, str(text), None

	if not text:
		if required:
			return False, None, _("This is a required field")
		return True, None, None

	if field_type in STRING_TYPES:
		return True, str(text), None

	if field_type in INT_TYPES:
		try:
			return True, int(text), None
		except ValueError:
			return False, None, _("Please enter a valid integer")

	if field_type in FLOAT_TYPES:
		try:
			return True, float(text), None
		except ValueError:
			return False, None, _("Please enter a valid number")

	if field_type == "regex":
		if re.match(detail_meta.get("options") or "", text):
			return True, text, None
		return False, None, _("Please enter a valid {0}").format(label)

	if field_type == "password":
		# Пароль пришёл открытым текстом: замазываем в базе и убираем из чата
		if context.telegram_message:
			context.telegram_message.mark_as_password()
		return True, text, None

	return False, None, _("Invalid type: {0}").format(field_type)
