// Поля типа Password на каждое нажатие клавиши дёргают
// frappe.core.doctype.user.user.test_password_strength. Для токена бота этот
// метод падает с "TypeError: Integer exceeds 64-bit range": zxcvbn возвращает
// число попыток подбора, которое для строки такой энтропии не влезает в int64,
// и orjson отказывается его сериализовать. Индикатор стойкости токену не нужен,
// поэтому просто выключаем проверку.
function disable_password_strength_check(frm) {
	for (const fieldname of ["api_token", "webhook_secret"]) {
		const field = frm.get_field(fieldname);
		field?.disable_password_checks?.();
	}
}

frappe.ui.form.on("Telegram Bot", {
	onload_post_render(frm) {
		disable_password_strength_check(frm);
	},

	refresh(frm) {
		disable_password_strength_check(frm);

		if (frm.is_new()) return;

		frm.dashboard.clear_headline();
		if (frm.doc.webhook_enabled) {
			frm.dashboard.set_headline(
				__("Webhook is active: {0}", [frm.doc.webhook_url || ""]),
				"green"
			);
		} else {
			frm.dashboard.set_headline(
				__("Webhook is not registered — the bot will not receive messages"),
				"orange"
			);
		}
	},

	mark_as_default(frm) {
		frm.call({ doc: frm.doc, method: "mark_as_default" }).then(() => frm.reload_doc());
	},

	set_webhook(frm) {
		frm.call({ doc: frm.doc, method: "set_webhook", freeze: true }).then(() =>
			frm.reload_doc()
		);
	},

	remove_webhook(frm) {
		frappe.confirm(__("Stop receiving updates for this bot?"), () => {
			frm.call({ doc: frm.doc, method: "remove_webhook", freeze: true }).then(() =>
				frm.reload_doc()
			);
		});
	},

	show_webhook_info(frm) {
		frm.call({ doc: frm.doc, method: "show_webhook_info", freeze: true });
	},
});
