app_name = "habibi_telegram"
app_title = "Habibi Telegram"
app_publisher = "DHI Partners"
app_description = "Telegram Bot Manager for Frappe"
app_email = "dosnet2200@gmail.com"
app_license = "MIT"

after_install = "habibi_telegram.setup.after_install"
after_migrate = "habibi_telegram.setup.after_migrate"

# Добавляет канал Telegram в стандартный Notification
override_doctype_class = {
	"Notification": "habibi_telegram.overrides.notification.TelegramNotification"
}

# ---------------------------------------------------------------------------
# Точки расширения
#
# telegram_bot_handler          — регистрация обработчиков входящих апдейтов.
#                                 Каждый пункт: def setup(registry, telegram_bot)
# telegram_update_pre_processors  — вызываются до обработчиков, для каждого апдейта
# telegram_update_post_processors — вызываются после обработчиков
# telegram_auth_handlers        — свой способ аутентификации вместо login/signup
# telegram_start_handler        — своя реакция на /start
# ---------------------------------------------------------------------------

telegram_bot_handler = [
	"habibi_telegram.handlers.start.setup",
	"habibi_telegram.handlers.auth.setup",
]

telegram_update_pre_processors = [
	"habibi_telegram.handlers.logging.pre_process",
]
