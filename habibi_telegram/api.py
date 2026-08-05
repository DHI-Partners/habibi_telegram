"""
Точка, в которую Telegram шлёт апдейты.

Вместо долгоживущего процесса с polling'ом — обычный whitelisted-метод Frappe.
Контейнеры остаются одноразовыми, ничего не надо держать запущенным, а
масштабируется это вместе с обычными веб-воркерами.

Адрес вебхука: /api/method/habibi_telegram.api.webhook?bot=<имя бота>
"""

import hmac
import json

import frappe
from frappe import _
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
