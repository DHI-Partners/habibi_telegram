"""
Канал Telegram для стандартного Notification.

Frappe рассылает уведомления по каналам Email / Slack / System / SMS; здесь
список расширяется каналом Telegram, а сама отправка уходит в очередь.

Если в своём приложении вы тоже переопределяете Notification, не забудьте
позвать send_telegram_notification, иначе канал перестанет работать.
"""

import frappe
from frappe.email.doctype.notification.notification import Notification, get_context

from habibi_telegram.client import send_file, send_message
from habibi_telegram.telegram_api import ParseMode

CHANNEL = "Telegram"


class TelegramNotification(Notification):
	def send(self, doc):
		if self.channel != CHANNEL:
			return super().send(doc)

		return send_telegram_notification(notification=self, doc=doc)


def send_telegram_notification(notification, doc):
	if notification.channel != CHANNEL:
		return

	context = get_context(doc)
	context.update({"doc": doc, "alert": notification, "comments": None})

	if doc.get("_comments"):
		context["comments"] = frappe.parse_json(doc.get("_comments"))

	if notification.is_standard:
		notification.load_standard_properties(context)

	message_text = frappe.render_template(notification.message, context)
	from_bot = notification.get("bot_to_send_from")

	print_file = None
	if notification.attach_print:
		attachment = notification.get_attachment(doc)[0]
		attachment.pop("print_format_attachment", None)
		print_file = frappe.attach_print(**attachment)

	for user in get_recipients(notification=notification, doc=doc, context=context):
		if not frappe.db.exists("Telegram User", {"user": user}):
			continue

		frappe.enqueue(
			send_message,
			queue="short",
			message_text=message_text,
			user=user,
			from_bot=from_bot,
			parse_mode=ParseMode.HTML,
			automated=True,
			enqueue_after_commit=True,
		)

		if print_file:
			frappe.enqueue(
				send_file,
				queue="short",
				file=print_file.get("fcontent"),
				filename=print_file.get("fname"),
				user=user,
				from_bot=from_bot,
				automated=True,
				enqueue_after_commit=True,
			)


def get_recipients(notification, doc, context) -> list:
	recipients = []

	for recipient in notification.recipients:
		if recipient.condition and not frappe.safe_eval(recipient.condition, None, context):
			continue

		if recipient.receiver_by_document_field:
			fields = recipient.receiver_by_document_field.split(",")

			if len(fields) > 1:
				# поле в дочерней таблице: "user_field,child_table"
				for row in doc.get(fields[1]) or []:
					user = row.get(fields[0])
					if user and frappe.db.exists("User", user):
						recipients.append(user)
			else:
				user = doc.get(fields[0])
				if user and frappe.db.exists("User", user):
					recipients.append(user)

		if recipient.receiver_by_role:
			recipients.extend(
				x.parent
				for x in frappe.get_all(
					"Has Role",
					filters={"role": recipient.receiver_by_role, "parenttype": "User"},
					fields=["parent"],
				)
			)

	return list(set(recipients))
