import urllib.parse

import frappe
from frappe import _
from frappe.model.document import Document

from habibi_telegram.constants import DEFAULT_TELEGRAM_BOT_KEY
from habibi_telegram.telegram_api import TelegramAPIError, TelegramBotAPI

WEBHOOK_METHOD = "habibi_telegram.api.webhook"

# Хосты, до которых Telegram не достучится
LOCAL_SUFFIXES = (".localhost", ".local", ".test", ".internal")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _is_local(host: str) -> bool:
	host = host.split(":", 1)[0].lower()

	return host in LOCAL_HOSTS or host.endswith(LOCAL_SUFFIXES)


class TelegramBot(Document):
	def autoname(self):
		# Имя уезжает в URL вебхука, поэтому без пробелов
		self.name = self.title.strip().replace(" ", "-")

	def validate(self):
		self.validate_api_token()

	def after_insert(self):
		if not frappe.db.get_default(DEFAULT_TELEGRAM_BOT_KEY):
			self.mark_as_default()

	def on_trash(self):
		if frappe.db.get_default(DEFAULT_TELEGRAM_BOT_KEY) != self.name:
			return

		frappe.db.set_default(DEFAULT_TELEGRAM_BOT_KEY, "")
		successor = frappe.db.get_value("Telegram Bot", {"name": ("!=", self.name)})
		if successor:
			frappe.db.set_default(DEFAULT_TELEGRAM_BOT_KEY, successor)
			frappe.db.set_value("Telegram Bot", successor, "is_default", 1)
			frappe.msgprint(_("{0} is now the default bot for notifications.").format(successor))

	def validate_api_token(self):
		"""Токен проверяем обращением к getMe — заодно подтягиваем username."""
		if not self.is_new() and not self.has_value_changed("api_token"):
			return

		try:
			me = self.get_api().get_me()
		except TelegramAPIError as e:
			frappe.throw(_("Error with Bot Token: {0}").format(str(e)))

		if not me.get("is_bot"):
			frappe.throw(_("This token belongs to a Telegram user, not a bot"))

		self.username = "@" + me.get("username", "")

	# -- вспомогательное ----------------------------------------------------

	def get_api(self) -> TelegramBotAPI:
		# при первом сохранении пароль ещё не в хранилище, берём из документа
		token = self.get_password("api_token") if not self.is_new() else self.api_token
		return TelegramBotAPI(token=token, bot_name=self.name)

	def get_webhook_url(self) -> str:
		"""
		Адрес собирается из имени сайта — отдельно домен нигде не настраивается.

		Имя сайта в Frappe и есть домен: nginx резолвит сайт по заголовку Host.
		Схема всегда https: TLS терминируется на прокси, поэтому сам frappe
		видит http и `get_url()` вернул бы неподходящий Telegram адрес. Если
		сайт и домен всё же разошлись — переопределяется через host_name
		в site_config.json.
		"""
		host = (frappe.conf.get("host_name") or frappe.local.site).rstrip("/")
		host = host.split("://", 1)[-1]

		if _is_local(host):
			frappe.throw(
				_(
					"Site '{0}' is not reachable from the internet, so Telegram cannot deliver updates. "
					"Register the webhook on the production site, or expose this one through a tunnel "
					"and set host_name in site_config.json."
				).format(host)
			)

		return f"https://{host}/api/method/{WEBHOOK_METHOD}?bot={urllib.parse.quote(self.name)}"

	# -- кнопки формы -------------------------------------------------------

	@frappe.whitelist()
	def mark_as_default(self):
		frappe.db.set_default(DEFAULT_TELEGRAM_BOT_KEY, self.name)
		frappe.db.set_value("Telegram Bot", {"name": ("!=", self.name)}, "is_default", 0)
		frappe.db.set_value("Telegram Bot", self.name, "is_default", 1)
		self.reload()
		frappe.msgprint(_("{0} is now the default bot for notifications.").format(self.title))

	@frappe.whitelist()
	def set_webhook(self):
		"""
		Зарегистрировать вебхук в Telegram.

		Telegram принимает только https и только публично доступный адрес,
		поэтому на localhost это работать не будет — нужен туннель либо прод.
		"""
		if self.is_new():
			# Имя бота уезжает в адрес вебхука, а у несохранённого документа оно
			# временное (new-telegram-bot-xxxx). Зарегистрировали бы адрес,
			# по которому потом никого нет.
			frappe.throw(_("Save the bot before registering its webhook"))

		url = self.get_webhook_url()

		secret = self.get_password("webhook_secret", raise_exception=False)
		if not secret:
			secret = frappe.generate_hash(length=32)
			self.webhook_secret = secret
			self.save(ignore_permissions=True)

		self.get_api().set_webhook(
			url=url,
			secret_token=secret,
			allowed_updates=["message", "edited_message", "callback_query"],
		)

		self.db_set({"webhook_url": url, "webhook_enabled": 1})
		frappe.msgprint(_("Webhook registered: {0}").format(url))

	@frappe.whitelist()
	def remove_webhook(self):
		self.get_api().delete_webhook()
		self.db_set({"webhook_enabled": 0})
		frappe.msgprint(_("Webhook removed"))

	@frappe.whitelist()
	def show_webhook_info(self):
		info = self.get_api().get_webhook_info()
		rows = "".join(
			f"<tr><td><b>{frappe.utils.escape_html(str(k))}</b></td>"
			f"<td>{frappe.utils.escape_html(str(v))}</td></tr>"
			for k, v in info.items()
		)
		frappe.msgprint(f"<table class='table table-bordered'>{rows}</table>", title=_("Webhook Info"))

		return info
