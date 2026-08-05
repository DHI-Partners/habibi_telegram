from frappe import _


def get_data():
	"""История чата — обычным списком Telegram Message, без самописной страницы."""
	return {
		"fieldname": "chat",
		"transactions": [
			{
				"label": _("Activity"),
				"items": ["Telegram Message"],
			}
		],
	}
