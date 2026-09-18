"""
Команды bench: `bench --site <site> telegram <команда>`.

Подключаются через точку входа в pyproject/hooks — frappe ищет переменную
`commands` в модуле <app>.commands.
"""

import click
import frappe
from frappe.commands import get_site, pass_context


@click.group("telegram")
def telegram():
	"""Управление телеграм-ботами"""
	pass


@click.command("list-bots")
@pass_context
def list_bots(context):
	"""Показать всех ботов сайта и состояние их вебхуков"""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()

	bots = frappe.get_all(
		"Telegram Bot", fields=["name", "username", "webhook_enabled", "webhook_url"]
	)
	click.echo(f"Telegram Bots: {len(bots)}")
	for bot in bots:
		state = "webhook on " if bot.webhook_enabled else "webhook off"
		click.echo(f"- {bot.name} ({bot.username or '—'}) [{state}] {bot.webhook_url or ''}")

	frappe.destroy()


@click.command("set-webhook")
@click.argument("telegram_bot")
@pass_context
def set_webhook(context, telegram_bot):
	"""Зарегистрировать вебхук бота в Telegram"""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()

	doc = frappe.get_doc("Telegram Bot", telegram_bot)
	doc.set_webhook()
	frappe.db.commit()
	click.echo(f"Webhook: {doc.get_webhook_url()}")

	frappe.destroy()


@click.command("remove-webhook")
@click.argument("telegram_bot")
@pass_context
def remove_webhook(context, telegram_bot):
	"""Отключить вебхук: бот перестанет получать сообщения"""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()

	frappe.get_doc("Telegram Bot", telegram_bot).remove_webhook()
	frappe.db.commit()
	click.echo("Webhook removed")

	frappe.destroy()


@click.command("webhook-info")
@click.argument("telegram_bot")
@pass_context
def webhook_info(context, telegram_bot):
	"""Что о вебхуке думает сам Telegram — полезно при разборе тишины"""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()

	info = frappe.get_doc("Telegram Bot", telegram_bot).get_api().get_webhook_info()
	for key, value in info.items():
		click.echo(f"{key}: {value}")

	frappe.destroy()


@click.command("list-accounts")
@pass_context
def list_accounts(context):
	"""Показать личные аккаунты сайта и состояние их синхронизации"""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()

	accounts = frappe.get_all(
		"Telegram Account",
		fields=["name", "username", "phone", "status", "sync_enabled", "last_sync_on"],
	)
	click.echo(f"Telegram Accounts: {len(accounts)}")
	for account in accounts:
		sync = "sync on " if account.sync_enabled else "sync off"
		click.echo(
			f"- {account.name} ({account.username or account.phone}) "
			f"[{account.status}, {sync}] last sync: {account.last_sync_on or '—'}"
		)

	frappe.destroy()


@click.command("sync-account")
@click.argument("telegram_account")
@pass_context
def sync_account(context, telegram_account):
	"""Забрать всё новое по аккаунту прямо сейчас"""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()

	from habibi_telegram.user_client import sync_account as sync

	stats = sync(telegram_account)
	frappe.db.commit()
	click.echo(f"new: {stats.get('new', 0)}, edited: {stats.get('edited', 0)}, deleted: {stats.get('deleted', 0)}")

	frappe.destroy()


@click.command("listen")
@click.argument("telegram_account")
@pass_context
def listen(context, telegram_account):
	"""
	Слушать апдейты аккаунта в открытом соединении.

	Обычной установке не нужно: планировщик и так забирает всё раз в минуту.
	Команда для тех, кому эта минута дорога — процесс вешается в supervisor.
	"""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()

	from habibi_telegram.user_client import listen as run_listener

	click.echo(f"Listening for {telegram_account}. Ctrl-C to stop.")
	try:
		run_listener(telegram_account)
	except KeyboardInterrupt:
		click.echo("Stopped")

	frappe.destroy()


@click.command("listen-all")
def listen_all():
	"""
	Слушать все подключённые аккаунты всех сайтов бенча.

	Для отдельного сервиса в compose: один процесс на бенч, аккаунты
	подхватываются и отпускаются сами, раз в минуту.
	"""
	from habibi_telegram.listener import run_all

	click.echo("Listening for all Telegram accounts. Ctrl-C to stop.")
	try:
		run_all()
	except KeyboardInterrupt:
		click.echo("Stopped")


telegram.add_command(list_bots)
telegram.add_command(set_webhook)
telegram.add_command(remove_webhook)
telegram.add_command(webhook_info)
telegram.add_command(list_accounts)
telegram.add_command(sync_account)
telegram.add_command(listen)
telegram.add_command(listen_all)

commands = [telegram]
