"""
Связка телеграм-аккаунта с пользователем Frappe.

Работает до всех остальных обработчиков (группа -100). Пока Telegram User не
связан с User, цепочка прерывается и пользователю показываются кнопки входа.

Своя схема аутентификации подключается хуком:

	telegram_auth_handlers = ["my_app.telegram.auth"]

Функция получает context и должна вернуть не-None, если аутентификацию взяла
на себя.
"""

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.password import check_password

from habibi_telegram.dispatcher import StopHandling
from habibi_telegram.utils import update as u
from habibi_telegram.utils.conversation import collect_conversation_details, reset

AUTH_HANDLER_GROUP = -100

# Постоянные значения, а не сгенерированные при импорте: callback_data живёт
# в кнопке у пользователя в чате и должна пережить перезапуск воркера
LOGIN_CALLBACK = "habibi_telegram:login"
SIGNUP_CALLBACK = "habibi_telegram:signup"

FLOW_KEY = "_auth_flow"
LOGIN_DETAILS = "login_details"
SIGNUP_DETAILS = "signup_details"

EMAIL_PATTERN = r"^.+@.+\..+$"


def setup(registry, telegram_bot):
	registry.any(authenticate, group=AUTH_HANDLER_GROUP)


def authenticate(context):
	frappe.set_user("Guest")

	telegram_user = context.telegram_user
	if not telegram_user:
		raise StopHandling

	if telegram_user.user:
		frappe.set_user(telegram_user.user)
		return

	if telegram_user.is_guest:
		return

	for method in reversed(frappe.get_hooks("telegram_auth_handlers") or []):
		if frappe.get_attr(method)(context) is not None:
			return

	flow = _resolve_flow(context)
	if not flow:
		_prompt(context)
		raise StopHandling

	if flow == "login":
		_collect_login(context)
	else:
		_collect_signup(context)

	raise StopHandling


def _resolve_flow(context) -> str | None:
	"""Какой сценарий сейчас идёт: начатый кнопкой или продолжающийся."""
	state = context.get_state()
	data = u.callback_data(context.update)

	if data in (LOGIN_CALLBACK, SIGNUP_CALLBACK):
		flow = "login" if data == LOGIN_CALLBACK else "signup"
		state[FLOW_KEY] = flow
		context.set_state(state)
		context.answer_callback()
		return flow

	return state.get(FLOW_KEY)


def _prompt(context):
	buttons = [{"text": _("Login"), "callback_data": LOGIN_CALLBACK}]

	if not cint(frappe.db.get_single_value("Website Settings", "disable_signup")):
		buttons.append({"text": _("Signup"), "callback_data": SIGNUP_CALLBACK})

	context.reply(
		_("Hi, please authenticate first before you continue"),
		reply_markup={"inline_keyboard": [buttons]},
	)


def _finish(context, user: str, message: str):
	context.telegram_user.db_set("user", user)
	context.telegram_user.clear_conversation_state()
	frappe.set_user(user)
	context.reply(message)


def _collect_login(context):
	details = collect_conversation_details(
		key=LOGIN_DETAILS,
		meta=[
			{"key": "email", "label": "Email", "type": "regex", "options": EMAIL_PATTERN},
			{"key": "pwd", "label": "Password", "type": "password"},
		],
		context=context,
	)
	if not details.get("_is_complete"):
		return

	user = verify_credentials(details.email, details.pwd)

	if not user:
		context.reply(_("You have entered invalid credentials. Please try again"))
		# Сбрасываем и тут же задаём первый вопрос заново: иначе бот замолкает,
		# и следующее сообщение пользователя уходит впустую
		reset(LOGIN_DETAILS, context)
		_collect_login(context)
		return

	_finish(context, user, _("You have successfully logged in as: {0}").format(user))


def _collect_signup(context):
	if cint(frappe.db.get_single_value("Website Settings", "disable_signup")):
		context.reply(_("Signup is disabled on this site"))
		context.telegram_user.clear_conversation_state()
		return

	details = collect_conversation_details(
		key=SIGNUP_DETAILS,
		meta=[
			{"key": "first_name", "label": "First Name", "type": "str"},
			{"key": "last_name", "label": "Last Name", "type": "str"},
			{"key": "email", "label": "Email", "type": "regex", "options": EMAIL_PATTERN},
			{"key": "pwd", "label": "Password", "type": "password"},
		],
		context=context,
	)
	if not details.get("_is_complete"):
		return

	if frappe.db.exists("User", details.email):
		context.reply(_("A user with this email already exists. Try logging in instead."))
		reset(SIGNUP_DETAILS, context)
		_collect_signup(context)
		return

	user = frappe.get_doc(
		doctype="User",
		email=details.email,
		first_name=details.first_name,
		last_name=details.last_name,
		enabled=1,
		new_password=details.pwd,
		send_welcome_email=0,
	)
	user.flags.telegram_user_signup = True
	user.insert(ignore_permissions=True)

	_finish(context, user.name, _("You have successfully signed up as: {0}").format(user.name))


def verify_credentials(email: str, pwd: str) -> str | None:
	"""Вернуть имя пользователя при верных данных, иначе None."""
	if not email or not pwd:
		return None

	if not frappe.db.exists("User", email):
		return None

	if not frappe.db.get_value("User", email, "enabled"):
		return None

	try:
		check_password(email, pwd)
	except frappe.AuthenticationError:
		return None

	return email
