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


telegram.add_command(list_bots)
telegram.add_command(set_webhook)
telegram.add_command(remove_webhook)
telegram.add_command(webhook_info)

commands = [telegram]
