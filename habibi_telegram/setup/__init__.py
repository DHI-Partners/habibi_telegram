import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from habibi_telegram.constants import ROLE_MANAGER, ROLE_USER, WORKSPACE, WORKSPACE_CARD


def after_install():
	setup_app()


def after_migrate():
	setup_app()


def setup_app():
	create_roles()
	add_telegram_notification_channel()
	add_notification_custom_fields()
	ensure_workspace()


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


def ensure_workspace():
	"""
	Раздел «Habibi Telegram» в боковом меню.

	Дописываем только недостающие ссылки и только в свою карточку: рабочее
	пространство пользователи правят руками, и затирать эти правки при каждой
	миграции нельзя. Своего файла-фикстуры у приложения поэтому нет.
	"""
	try:
		_ensure_workspace()
	except Exception:
		# Раздел — украшение, ради него ронять установку незачем: сами DocType
		# доступны и через поиск
		frappe.log_error(title="Telegram workspace setup failed", message=frappe.get_traceback())


def _ensure_workspace():
	import json

	doctypes = [
		"Telegram Bot",
		"Telegram Account",
		"Telegram Chat",
		"Telegram User",
		"Telegram Message",
		"Telegram Message Template",
	]

	if frappe.db.exists("Workspace", WORKSPACE):
		doc = frappe.get_doc("Workspace", WORKSPACE)
	else:
		doc = frappe.new_doc("Workspace")
		doc.update(
			{
				"name": WORKSPACE,
				"label": WORKSPACE,
				"title": WORKSPACE,
				"module": "Habibi Telegram",
				"icon": "message",
				"public": 1,
			}
		)

	rows = list(doc.get("links") or [])
	linked = {row.link_to for row in rows if row.get("type") == "Link"}
	missing = [dt for dt in doctypes if dt not in linked]

	if not missing and not doc.is_new():
		return

	# Куда вставлять: сразу за последней строкой своей карточки, а если её ещё
	# нет — в конец, вместе с самим заголовком карточки
	positions = [i for i, row in enumerate(rows) if row.get("label") == WORKSPACE_CARD]

	if positions:
		start = positions[0]
		insert_at = len(rows)
		for i in range(start + 1, len(rows)):
			if rows[i].get("type") == "Card Break":
				insert_at = i
				break
	else:
		rows.append(
			frappe._dict(
				{"type": "Card Break", "label": WORKSPACE_CARD, "hidden": 0, "onboard": 0}
			)
		)
		insert_at = len(rows)

	new_rows = [
		frappe._dict(
			{
				"type": "Link",
				"label": dt,
				"link_type": "DocType",
				"link_to": dt,
				"hidden": 0,
				"onboard": 0,
			}
		)
		for dt in missing
	]
	rows[insert_at:insert_at] = new_rows

	doc.set("links", [])
	for row in rows:
		doc.append("links", dict(row))

	# Карточка рисуется по блоку в content — без него ссылки в разделе не видны
	try:
		content = json.loads(doc.content or "[]")
	except ValueError:
		content = []

	has_card = any(
		block.get("type") == "card" and block.get("data", {}).get("card_name") == WORKSPACE_CARD
		for block in content
	)
	if not has_card:
		content.append(
			{"id": "habibi-telegram-card", "type": "card", "data": {"card_name": WORKSPACE_CARD, "col": 4}}
		)
	doc.content = json.dumps(content)

	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)


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
