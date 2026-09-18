"""Снять «бота по умолчанию» с сообщений личных аккаунтов.

До исправления в TelegramMessage.before_insert frappe подставлял глобальный
default telegram_bot в одноимённое поле каждого нового сообщения, и записи
аккаунтов выглядели пришедшими ещё и через бота.
"""

import frappe


def execute():
	frappe.db.sql(
		"""
		update `tabTelegram Message`
		set telegram_bot = null
		where telegram_account is not null and telegram_account != ''
			and telegram_bot is not null
		"""
	)
