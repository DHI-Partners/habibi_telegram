import frappe


def setup(registry, telegram_bot):
	registry.command("start", start_handler)


def start_handler(context):
	"""
	Реакция на /start. Своё поведение подставляется хуком telegram_start_handler:

		telegram_start_handler = ["my_app.telegram.start"]
	"""
	override = frappe.get_hooks("telegram_start_handler")
	if override:
		return frappe.get_attr(override[-1])(context)

	if frappe.session.user == "Guest":
		# Сюда мы не доходим: обработчик аутентификации уже прервал цепочку
		return

	first_name = frappe.db.get_value("User", frappe.session.user, "first_name")
	context.reply(frappe._("Welcome!"))
	context.reply(frappe._("You are logged in as: {0}").format(first_name or frappe.session.user))
