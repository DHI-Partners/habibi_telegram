import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from habibi_telegram.constants import ROLE_MANAGER, ROLE_USER


def after_install():
	setup_app()


def after_migrate():
	setup_app()


def setup_app():
	create_roles()
	add_telegram_notification_channel()
	add_notification_custom_fields()


def create_roles():
	for role in (ROLE_MANAGER, ROLE_USER):
		if frappe.db.exists("Role", role):
			continue

		frappe.get_doc(doctype="Role", role_name=role, desk_access=1).insert(
			ignore_permissions=True
		)


def add_telegram_notification_channel():
	"""
	Дописывает Telegram в список каналов стандартного Notification.

	Через Property Setter, а не правкой самого DocType: так не затираются
	каналы, добавленные другими приложениями.
	"""
	channels = frappe.get_meta("Notification").get_field("channel").options.split("\n")
	if "Telegram" in channels:
		return

	channels.append("Telegram")

	existing = frappe.db.exists(
		"Property Setter",
		{"doc_type": "Notification", "field_name": "channel", "property": "options"},
	)

	if existing:
		frappe.db.set_value("Property Setter", existing, "value", "\n".join(channels))
	else:
		frappe.get_doc(
			doctype="Property Setter",
			doctype_or_field="DocField",
			doc_type="Notification",
			field_name="channel",
			property="options",
			value="\n".join(channels),
			property_type="Small Text",
		).insert(ignore_permissions=True)

	frappe.clear_cache(doctype="Notification")


def add_notification_custom_fields():
	create_custom_fields(
		{
			"Notification": [
				{
					"fieldname": "bot_to_send_from",
					"label": "Bot to Send From",
					"fieldtype": "Link",
					"options": "Telegram Bot",
					"insert_after": "channel",
					"depends_on": "eval:doc.channel=='Telegram'",
					"description": "Если не заполнено — берётся бот по умолчанию",
				}
			]
		},
		ignore_validate=True,
	)
