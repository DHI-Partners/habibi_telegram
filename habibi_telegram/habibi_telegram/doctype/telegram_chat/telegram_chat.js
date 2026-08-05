frappe.ui.form.on("Telegram Chat", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Send Message"), () => send_message_dialog(frm));
		frm.change_custom_button_type(__("Send Message"), null, "primary");

		frm.add_custom_button(__("Messages"), () =>
			frappe.set_route("List", "Telegram Message", { chat: frm.doc.name })
		);
	},
});

function send_message_dialog(frm) {
	const bots = (frm.doc.bots || []).map((row) => row.telegram_bot);

	const dialog = new frappe.ui.Dialog({
		title: __("Send Message to {0}", [frm.doc.title]),
		fields: [
			{
				fieldname: "message",
				label: __("Message"),
				fieldtype: "Text",
				reqd: 1,
				description: __("Up to 4096 characters"),
			},
			{
				fieldname: "parse_mode",
				label: __("Formatting"),
				fieldtype: "Select",
				options: [
					{ value: "", label: __("Plain text") },
					{ value: "HTML", label: "HTML" },
					{ value: "MarkdownV2", label: "MarkdownV2" },
				],
				default: "",
			},
			{
				fieldname: "from_bot",
				label: __("Send From"),
				fieldtype: "Link",
				options: "Telegram Bot",
				default: bots[0],
				description: __("Leave empty to use the default bot"),
			},
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			dialog.disable_primary_action();

			frappe
				.call({
					method: "habibi_telegram.client.send_chat_message",
					args: {
						chat: frm.doc.name,
						message: values.message,
						parse_mode: values.parse_mode || null,
						from_bot: values.from_bot || null,
					},
				})
				.then(() => {
					dialog.hide();
					frappe.show_alert({ message: __("Message sent"), indicator: "green" });
					frm.reload_doc();
				})
				.always(() => dialog.enable_primary_action());
		},
	});

	dialog.show();
}
