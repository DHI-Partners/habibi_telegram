frappe.ui.form.on("Telegram Bot", {
	refresh(frm) {
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
