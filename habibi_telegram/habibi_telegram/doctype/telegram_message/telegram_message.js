// Правка и удаление идут в Telegram, а не только в базу: кто именно их
// выполнит — бот или личный аккаунт — решает контроллер (get_transport).

frappe.ui.form.on("Telegram Message", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.dashboard.clear_headline();

		if (frm.doc.is_deleted) {
			frm.dashboard.set_headline(__("Deleted in Telegram"), "red");
		} else if (frm.doc.is_edited) {
			frm.dashboard.set_headline(__("Edited"), "orange");
		}

		if (!frm.doc.is_deleted && frm.doc.direction === "Outgoing") {
			frm.add_custom_button(__("Edit in Telegram"), () => edit_dialog(frm));
			frm.change_custom_button_type(__("Edit in Telegram"), null, "primary");
		}

		if (!frm.doc.is_deleted) {
			frm.add_custom_button(__("Delete in Telegram"), () => delete_dialog(frm));
		}

		frm.add_custom_button(__("Open Chat"), () =>
			frappe.set_route("Form", "Telegram Chat", frm.doc.chat)
		);
	},
});

function edit_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Edit Message"),
		fields: [
			{
				fieldname: "content",
				label: __("Message"),
				fieldtype: "Text",
				reqd: 1,
				default: frm.doc.content,
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
				fieldtype: "HTML",
				options: `<div class="text-muted small">${__(
					"Telegram allows editing your own messages within 48 hours"
				)}</div>`,
			},
		],
		primary_action_label: __("Save"),
		primary_action(values) {
			dialog.disable_primary_action();

			frm.call({
				doc: frm.doc,
				method: "edit_content",
				args: { content: values.content, parse_mode: values.parse_mode || null },
				freeze: true,
			})
				.then(() => {
					dialog.hide();
					frappe.show_alert({ message: __("Message edited"), indicator: "green" });
					frm.reload_doc();
				})
				.always(() => dialog.enable_primary_action());
		},
	});

	dialog.show();
}

function delete_dialog(frm) {
	// «Только у себя» умеет личный аккаунт; бот всегда удаляет у всех
	const from_account = Boolean(frm.doc.telegram_account);

	const dialog = new frappe.ui.Dialog({
		title: __("Delete Message"),
		fields: [
			{
				fieldname: "revoke",
				label: __("Delete for everyone"),
				fieldtype: "Check",
				default: 1,
				read_only: from_account ? 0 : 1,
				description: from_account
					? __("Otherwise the message disappears only from this account")
					: __("A bot can only delete for everyone"),
			},
		],
		primary_action_label: __("Delete"),
		primary_action(values) {
			dialog.disable_primary_action();

			frm.call({
				doc: frm.doc,
				method: "delete_in_telegram",
				args: { revoke: values.revoke ? 1 : 0 },
				freeze: true,
			})
				.then(() => {
					dialog.hide();
					frappe.show_alert({ message: __("Message deleted"), indicator: "green" });
					frm.reload_doc();
				})
				.always(() => dialog.enable_primary_action());
		},
	});

	dialog.show();
}
