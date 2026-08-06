// Поля типа Password на каждое нажатие клавиши дёргают проверку стойкости
// пароля, а она падает на строках такой энтропии (см. комментарий в
// telegram_bot.js). Ни api_hash, ни ключ сессии в индикаторе не нуждаются.
function disable_password_strength_check(frm) {
	for (const fieldname of ["api_hash", "session_string"]) {
		frm.get_field(fieldname)?.disable_password_checks?.();
	}
}

const STATUS_INDICATOR = {
	Connected: "green",
	"Code Sent": "orange",
	"Password Required": "orange",
	Disconnected: "red",
};

function set_headline(frm) {
	frm.dashboard.clear_headline();

	if (frm.is_new()) return;

	const messages = {
		Connected: __("Connected as {0}. Everything the account receives lands in Telegram Message.", [
			frm.doc.username || frm.doc.full_name || frm.doc.phone,
		]),
		"Code Sent": __("Telegram has sent a code — press 'Sign In' and enter it"),
		"Password Required": __("Two-factor authentication is on — press 'Sign In' and enter the password"),
		Disconnected: __("Account is not connected — press 'Request Code'"),
	};

	frm.dashboard.set_headline(
		messages[frm.doc.status] || "",
		STATUS_INDICATOR[frm.doc.status] || "blue"
	);

	if (frm.doc.last_error) {
		frm.dashboard.set_headline(__("Last error: {0}", [frm.doc.last_error]), "red");
	}
}

// Код и пароль спрашиваем диалогом и передаём параметром: в базе им делать нечего
function ask_code(frm) {
	frappe.prompt(
		{
			fieldname: "code",
			label: __("Code from Telegram"),
			fieldtype: "Data",
			reqd: 1,
			description: __("Telegram sends it to the app, or by SMS if you are not signed in anywhere"),
		},
		({ code }) => submit_sign_in(frm, { code }),
		__("Confirm Sign In"),
		__("Sign In")
	);
}

function ask_password(frm) {
	frappe.prompt(
		{
			fieldname: "password",
			label: __("Two-Factor Password"),
			fieldtype: "Password",
			reqd: 1,
		},
		({ password }) => submit_sign_in(frm, { password }),
		__("Two-Factor Authentication"),
		__("Sign In")
	);
}

function submit_sign_in(frm, args) {
	frm.call({ doc: frm.doc, method: "sign_in", args, freeze: true }).then((r) => {
		if (r.message && r.message.password_required) {
			frm.reload_doc().then(() => ask_password(frm));
			return;
		}

		frappe.show_alert({ message: __("Account connected"), indicator: "green" });
		// Первая синхронизация уходит в фон, документ обновится сам чуть позже
		frm.reload_doc();
	});
}

frappe.ui.form.on("Telegram Account", {
	onload_post_render(frm) {
		disable_password_strength_check(frm);
	},

	refresh(frm) {
		disable_password_strength_check(frm);
		set_headline(frm);

		if (frm.is_new()) return;

		if (frm.doc.status === "Connected") {
			frm.add_custom_button(__("Send Message"), () => send_message_dialog(frm));
			frm.change_custom_button_type(__("Send Message"), null, "primary");
		}

		frm.add_custom_button(__("Messages"), () =>
			frappe.set_route("List", "Telegram Message", { telegram_account: frm.doc.name })
		);
	},

	request_code(frm) {
		frm.call({ doc: frm.doc, method: "request_code", freeze: true }).then(() => {
			frm.reload_doc().then(() => ask_code(frm));
		});
	},

	sign_in(frm) {
		frm.doc.status === "Password Required" ? ask_password(frm) : ask_code(frm);
	},

	log_out(frm) {
		frappe.confirm(
			__("Sign out of Telegram? The session will be revoked and the code will be needed again."),
			() => {
				frm.call({ doc: frm.doc, method: "log_out", freeze: true }).then(() =>
					frm.reload_doc()
				);
			}
		);
	},

	sync_now(frm) {
		frm.call({ doc: frm.doc, method: "sync_now", freeze: true }).then(() => frm.reload_doc());
	},

	fetch_dialogs(frm) {
		frm.call({ doc: frm.doc, method: "refresh_dialogs", freeze: true }).then(() =>
			frm.reload_doc()
		);
	},
});

function send_message_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Send from {0}", [frm.doc.username || frm.doc.title]),
		fields: [
			{
				fieldname: "chat",
				label: __("Chat"),
				fieldtype: "Link",
				options: "Telegram Chat",
				reqd: 1,
				description: __("Only chats this account has already seen"),
			},
			{
				fieldname: "message",
				label: __("Message"),
				fieldtype: "Text",
				reqd: 1,
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
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			dialog.disable_primary_action();

			frappe
				.call({
					method: "habibi_telegram.client.send_chat_message",
					args: {
						chat: values.chat,
						message: values.message,
						parse_mode: values.parse_mode || null,
						from_account: frm.doc.name,
					},
					freeze: true,
				})
				.then(() => {
					dialog.hide();
					frappe.show_alert({ message: __("Message sent"), indicator: "green" });
				})
				.always(() => dialog.enable_primary_action());
		},
	});

	dialog.show();
}
