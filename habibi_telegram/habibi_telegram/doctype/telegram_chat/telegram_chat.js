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
	const accounts = (frm.doc.accounts || []).map((row) => row.telegram_account);

	// В чат, где бота нет, писать можно только личным аккаунтом — и наоборот
	const send_as = bots.length || !accounts.length ? "Bot" : "Account";

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
				fieldname: "send_as",
				label: __("Send As"),
				fieldtype: "Select",
				options: [
					{ value: "Bot", label: __("Bot") },
					{ value: "Account", label: __("Personal account") },
				],
				default: send_as,
			},
			{
				fieldname: "from_bot",
				label: __("Bot"),
				fieldtype: "Link",
				options: "Telegram Bot",
				default: bots[0],
				depends_on: "eval:doc.send_as == 'Bot'",
				description: __("Leave empty to use the default bot"),
			},
			{
				fieldname: "from_account",
				label: __("Account"),
				fieldtype: "Link",
				options: "Telegram Account",
				default: accounts[0],
				depends_on: "eval:doc.send_as == 'Account'",
				get_query: () => ({ filters: { status: "Connected", enabled: 1 } }),
			},
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			const as_account = values.send_as === "Account";

			if (as_account && !values.from_account) {
				frappe.msgprint(__("Choose the account to send from"));
				return;
			}

			dialog.disable_primary_action();

			frappe
				.call({
					method: "habibi_telegram.client.send_chat_message",
					args: {
						chat: frm.doc.name,
						message: values.message,
						parse_mode: values.parse_mode || null,
						from_bot: as_account ? null : values.from_bot || null,
						from_account: as_account ? values.from_account : null,
					},
					freeze: true,
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
