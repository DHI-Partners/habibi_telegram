"""
Карточка собеседника для консоли чатов — то, что рисуется третьей колонкой.

Колонка живёт только в личной переписке. В группе объявлений собеседника нет:
там десятки авторов, и «завести лид по чату» ничего не значит — лид там растёт
из конкретного сообщения, а не из диалога. Поэтому для остальных типов чата
метод честно отвечает applicable: False, и страница отдаёт место переписке.

Связь с CRM хранится стандартной Dynamic Link — child-таблицей, которой то же
самое делают Contact и Address. Своего доктайпа для этого не нужно, а привязать
чат можно к чему угодно: Lead в ERPNext, CRM Lead во Frappe CRM, Opportunity,
Customer. Что из этого установлено на сайте, выясняется на месте.
"""

import frappe
from frappe import _

# Куда предлагаем завести запись, в порядке предпочтения. Показываем только то,
# что установлено и куда пускают права, — список сам подстраивается под сайт.
CRM_TARGETS = (
	"CRM Lead",
	"Lead",
	"CRM Deal",
	"Opportunity",
	"Contact",
	"Customer",
)

# Цвет для статусов, у которых доктайп не объявил Document State. Ключи — то,
# как статус называется в ERPNext и Frappe CRM; всё незнакомое остаётся синим.
STATUS_INDICATORS = {
	"Lead": "orange",
	"Open": "orange",
	"New": "orange",
	"Replied": "blue",
	"Contacted": "blue",
	"Nurture": "blue",
	"Interested": "blue",
	"Quotation": "blue",
	"Opportunity": "blue",
	"Negotiation": "purple",
	"Ready to Close": "purple",
	"Converted": "green",
	"Qualified": "green",
	"Won": "green",
	"Closed": "green",
	"Lost Quotation": "red",
	"Junk": "red",
	"Lost": "red",
	"Do Not Contact": "red",
	"Disabled": "gray",
}


@frappe.whitelist()
def get_chat_context(chat_id: str) -> dict:
	"""Всё, что показывает правая колонка: собеседник, связи с CRM, активность."""
	chat = frappe.get_doc("Telegram Chat", chat_id)
	chat.check_permission("read")

	if chat.type != "private":
		return {"applicable": False}

	return {
		"applicable": True,
		"contact": _contact(chat),
		"links": _links(chat),
		"targets": _targets(),
		"stats": _stats(chat),
		"can_link": bool(chat.has_permission("write")),
	}


@frappe.whitelist(methods=["POST"])
def new_doc_defaults(chat_id: str, doctype: str) -> dict:
	"""
	Чем заполнить форму создания.

	Поля подбираются по мете, а не по названию доктайпа: у Lead в ERPNext это
	first_name с lead_name, у CRM Lead — first_name с organization, у Customer —
	customer_name. Ставим то, что доктайп у себя нашёл.
	"""
	chat = frappe.get_doc("Telegram Chat", chat_id)
	chat.check_permission("read")
	_check_target(doctype)

	contact = _contact(chat) or {}
	full_name = (contact.get("full_name") or chat.title or "").strip()
	parts = full_name.split()

	meta = frappe.get_meta(doctype)
	candidates = {
		"first_name": parts[0] if parts else None,
		"last_name": " ".join(parts[1:]) or None,
		"lead_name": full_name or None,
		"customer_name": full_name or None,
	}

	values = {}
	for fieldname, value in candidates.items():
		field = meta.get_field(fieldname)

		# Только Data. Одно и то же имя поля у разных доктайпов значит разное:
		# у Lead lead_name — это имя человека, а у Customer то же lead_name —
		# ссылка на сам Lead, и запись с именем вместо ссылки не сохранится.
		# Read Only поля тоже мимо: их доктайп заполняет сам
		if value and field and field.fieldtype == "Data":
			values[fieldname] = value

	# Источник проставляем, только если он на сайте заведён: Lead Source —
	# обычный справочник, и «Telegram» в нём есть не у всех
	source = meta.get_field("source")
	if source and source.options and frappe.db.exists(source.options, "Telegram"):
		values["source"] = "Telegram"

	return values


@frappe.whitelist(methods=["POST"])
def link_record(chat_id: str, link_doctype: str, link_name: str) -> list[dict]:
	"""Привязать чат к записи CRM. Повторная привязка того же ничего не делает."""
	chat = frappe.get_doc("Telegram Chat", chat_id)
	chat.check_permission("write")

	_check_target(link_doctype)

	if not frappe.db.exists(link_doctype, link_name):
		frappe.throw(_("{0} {1} not found").format(_(link_doctype), link_name))

	frappe.has_permission(link_doctype, "read", doc=link_name, throw=True)

	if not _find_link(chat, link_doctype, link_name):
		chat.append("links", {"link_doctype": link_doctype, "link_name": link_name})
		chat.save()

	return _links(chat)


@frappe.whitelist(methods=["POST"])
def unlink_record(chat_id: str, link_doctype: str, link_name: str) -> list[dict]:
	"""Снять привязку. Саму запись CRM это не трогает."""
	chat = frappe.get_doc("Telegram Chat", chat_id)
	chat.check_permission("write")

	row = _find_link(chat, link_doctype, link_name)
	if row:
		chat.remove(row)
		chat.save()

	return _links(chat)


def _contact(chat) -> dict | None:
	"""
	Собеседник в личной переписке.

	Участники пополняются по мере того, как от человека приходят сообщения, так
	что в свежем чате таблица бывает пустой — тогда показывать нечего, кроме
	названия самого чата.
	"""
	row = chat.users[0] if chat.users else None
	if not row or not row.telegram_user:
		return None

	user = frappe.db.get_value(
		"Telegram User",
		row.telegram_user,
		["name", "full_name", "telegram_username", "telegram_user_id", "user", "is_guest"],
		as_dict=True,
	)

	if not user:
		return None

	return {
		"telegram_user": user.name,
		"full_name": user.full_name,
		"telegram_username": user.telegram_username,
		"telegram_user_id": user.telegram_user_id,
		"user": user.user,
		"is_guest": bool(user.is_guest),
	}


def _links(chat) -> list[dict]:
	"""Привязанные записи — с заголовком и статусом, как их рисует список."""
	rows = []

	for row in chat.get("links") or []:
		if not row.link_doctype or not row.link_name:
			continue

		if not frappe.db.exists("DocType", row.link_doctype):
			# Приложение с этим доктайпом с сайта сняли — показывать нечего
			continue

		if not frappe.has_permission(row.link_doctype, "read", doc=row.link_name):
			continue

		link = _link_payload(row.link_doctype, row.link_name)
		if link:
			rows.append(link)

	return rows


def _link_payload(doctype: str, name: str) -> dict | None:
	meta = frappe.get_meta(doctype)
	fields = ["name"]

	if meta.title_field and meta.title_field != "name":
		fields.append(meta.title_field)

	if meta.has_field("status"):
		fields.append("status")

	doc = frappe.db.get_value(doctype, name, fields, as_dict=True)
	if not doc:
		# Запись удалили, а строка привязки осталась
		return None

	status = doc.get("status")

	return {
		"doctype": doctype,
		"name": name,
		"title": (meta.title_field and doc.get(meta.title_field)) or name,
		"status": status,
		"indicator": _indicator(meta, status),
	}


def _indicator(meta, status: str) -> str:
	"""Цвет статуса: сперва то, что доктайп объявил сам, потом наша таблица."""
	if not status:
		return "gray"

	for state in meta.get("states") or []:
		if state.get("title") == status:
			return (state.get("color") or "gray").lower()

	return STATUS_INDICATORS.get(status, "blue")


def _targets() -> list[dict]:
	return [
		{"doctype": doctype, "label": _(doctype)}
		for doctype in CRM_TARGETS
		if frappe.db.exists("DocType", doctype) and frappe.has_permission(doctype, "create")
	]


def _check_target(doctype: str):
	"""
	Привязывать даём только к тому, что мы сами предложили.

	Иначе метод превращается в способ выяснить, какие документы есть на сайте:
	ошибка «не найдено» отличается от ошибки прав.
	"""
	if doctype not in CRM_TARGETS:
		frappe.throw(_("{0} cannot be linked to a Telegram chat").format(_(doctype)))


def _find_link(chat, link_doctype: str, link_name: str):
	for row in chat.get("links") or []:
		if row.link_doctype == link_doctype and row.link_name == link_name:
			return row

	return None


def _stats(chat) -> dict:
	"""Сколько всего написано и когда началось — три поля под карточкой."""
	first = frappe.db.get_value(
		"Telegram Message",
		{"chat": chat.name},
		["sent_on", "creation"],
		order_by="creation asc",
		as_dict=True,
	)

	return {
		"messages": frappe.db.count("Telegram Message", {"chat": chat.name}),
		"first_message_on": str(first.sent_on or first.creation) if first else None,
		"last_message_on": str(chat.last_message_on) if chat.last_message_on else None,
	}
