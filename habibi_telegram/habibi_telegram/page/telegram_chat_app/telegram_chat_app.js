// Консоль чатов: слева диалоги, справа переписка.
//
// Новые сообщения приезжают событием telegram_message из комнаты открытого
// Telegram Chat (его публикует TelegramMessage.after_insert). Поллинг рядом —
// не дубль, а подстраховка: он обновляет список диалогов целиком, в том числе
// по чатам, на которые мы не подписаны, и вытягивает пропущенное, если
// socket.io недоступен.

frappe.provide("habibi_telegram");

// Как часто дёргать сервер, когда socket.io молчит
const POLL_INTERVAL = 15000;

// Сколько сообщений тянем за раз
const PAGE_LENGTH = 50;

// Пользователь листает историю выше этой границы — дёргать его прокруткой
// вниз на каждое новое сообщение невежливо
const STICK_TO_BOTTOM_PX = 120;

frappe.pages["telegram-chat-app"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Telegram Chat Console"),
		single_column: true,
	});

	wrapper.chat_console = new habibi_telegram.ChatConsole(page);
};

frappe.pages["telegram-chat-app"].on_page_show = function (wrapper) {
	// Страница живёт между переходами по Desk: вернулись — освежаем
	wrapper.chat_console && wrapper.chat_console.refresh();
};

habibi_telegram.ChatConsole = class ChatConsole {
	constructor(page) {
		this.page = page;
		this.chats = [];
		this.messages = [];
		this.chat = null;
		this.search = "";
		this.has_more = false;
		this.sending = false;
		this.can_write = frappe.model.can_write("Telegram Chat");

		this.make_dom();
		this.bind_events();
		this.listen();
		this.start_polling();

		this.load_chats();
	}

	refresh() {
		this.load_chats({ silent: true });

		if (this.chat) {
			this.fetch_new_messages();
		}
	}

	// -- разметка -----------------------------------------------------------

	make_dom() {
		this.page.main.append(`
			<div class="telegram-console">
				<div class="tg-sidebar">
					<div class="tg-search">
						<input type="text" class="form-control tg-search-input"
							placeholder="${__("Search chats")}" autocomplete="off">
					</div>
					<div class="tg-chat-list"></div>
				</div>
				<div class="tg-conversation">
					<div class="tg-header"></div>
					<div class="tg-messages"></div>
					<div class="tg-composer"></div>
				</div>
			</div>
		`);

		this.$console = this.page.main.find(".telegram-console");
		this.$search = this.$console.find(".tg-search-input");
		this.$chat_list = this.$console.find(".tg-chat-list");
		this.$header = this.$console.find(".tg-header");
		this.$messages = this.$console.find(".tg-messages");
		this.$composer = this.$console.find(".tg-composer");

		this.render_conversation();

		this.page.set_secondary_action(__("Refresh"), () => this.refresh(), "refresh");
	}

	bind_events() {
		this.$search.on(
			"input",
			frappe.utils.debounce(() => {
				this.search = this.$search.val();
				this.load_chats();
			}, 300)
		);

		this.$chat_list.on("click", ".tg-chat", (e) => {
			this.open_chat($(e.currentTarget).attr("data-chat"));
		});

		this.$messages.on("click", ".tg-load-more", () => this.load_older_messages());

		// Композер перерисовывается вместе с чатом, поэтому слушаем контейнер
		this.$composer.on("click", ".tg-send", () => this.send());

		this.$composer.on("keydown", ".tg-input", (e) => {
			// Enter отправляет, Shift+Enter переносит строку
			if (e.key === "Enter" && !e.shiftKey) {
				e.preventDefault();
				this.send();
			}
		});

		this.$composer.on("input", ".tg-input", (e) => this.autosize(e.currentTarget));
	}

	listen() {
		frappe.realtime.on("telegram_message", (message) => {
			if (!message || !message.chat) return;

			if (message.chat === this.chat) {
				this.append_message(message);
			}

			this.load_chats({ silent: true });
		});
	}

	start_polling() {
		setInterval(() => {
			// Страница остаётся в DOM после ухода на другой роут — незачем
			// ходить на сервер, пока её не видно
			if (!$(this.page.wrapper).is(":visible") || document.hidden) return;

			this.refresh();
		}, POLL_INTERVAL);
	}

	// -- список диалогов ----------------------------------------------------

	load_chats({ silent = false } = {}) {
		return frappe
			.call({
				method: "habibi_telegram.api.get_chats",
				args: { search: this.search || null },
			})
			.then((r) => {
				this.chats = r.message || [];
				this.render_chats();
			})
			.catch((e) => {
				if (!silent) throw e;
			});
	}

	render_chats() {
		if (!this.chats.length) {
			this.$chat_list.html(
				`<div class="tg-empty">${
					this.search ? __("Nothing found") : __("No chats yet")
				}</div>`
			);
			return;
		}

		this.$chat_list.html(this.chats.map((chat) => this.chat_html(chat)).join(""));
	}

	chat_html(chat) {
		const title = chat.title || chat.chat_id;

		return `
			<div class="tg-chat ${chat.name === this.chat ? "active" : ""}"
				data-chat="${frappe.utils.escape_html(chat.name)}">
				<div class="tg-avatar">${frappe.utils.escape_html(this.initials(title))}</div>
				<div class="tg-chat-body">
					<div class="tg-chat-top">
						<span class="tg-chat-title">${frappe.utils.escape_html(title)}</span>
						<span class="tg-chat-time">${this.short_time(chat.last_message_on)}</span>
					</div>
					<div class="tg-chat-preview">${frappe.utils.escape_html(
						chat.last_message || ""
					)}</div>
				</div>
			</div>
		`;
	}

	// -- переписка ----------------------------------------------------------

	open_chat(name) {
		if (!name || name === this.chat) return;

		if (this.chat) {
			frappe.realtime.doc_unsubscribe("Telegram Chat", this.chat);
		}

		this.chat = name;
		this.messages = [];
		// Подписку проверяет сервер: в комнату чужого чата не пустят
		frappe.realtime.doc_subscribe("Telegram Chat", this.chat);

		this.render_chats();
		this.render_conversation();
		this.load_messages();
	}

	current_chat() {
		return this.chats.find((chat) => chat.name === this.chat);
	}

	load_messages() {
		this.$messages.html(`<div class="tg-empty">${__("Loading...")}</div>`);

		return frappe
			.call({
				method: "habibi_telegram.api.get_messages",
				args: { chat_id: this.chat, limit: PAGE_LENGTH },
			})
			.then((r) => {
				const messages = r.message || [];

				this.has_more = messages.length === PAGE_LENGTH;
				this.messages = messages;
				this.render_messages();
				this.scroll_to_bottom();
				this.focus_input();
			});
	}

	load_older_messages() {
		this.$messages.find(".tg-load-more").prop("disabled", true).text(__("Loading..."));

		// Высота до догрузки: после неё вернём пользователя на то же место
		const before = this.$messages[0].scrollHeight;

		return frappe
			.call({
				method: "habibi_telegram.api.get_messages",
				args: {
					chat_id: this.chat,
					limit: PAGE_LENGTH,
					start: this.messages.length,
				},
			})
			.then((r) => {
				const older = r.message || [];

				this.has_more = older.length === PAGE_LENGTH;
				this.messages = older.concat(this.messages);
				this.render_messages();

				this.$messages[0].scrollTop = this.$messages[0].scrollHeight - before;
			});
	}

	fetch_new_messages() {
		return frappe
			.call({
				method: "habibi_telegram.api.get_messages",
				args: { chat_id: this.chat, after: this.cursor() || null, limit: PAGE_LENGTH },
			})
			.then((r) => (r.message || []).forEach((message) => this.append_message(message)))
			.catch(() => {
				// Чат могли удалить или закрыть к нему доступ; это фон, молчим
			});
	}

	cursor() {
		// Догружаем по creation, а не по времени Telegram: аккаунт приносит
		// старую переписку задним числом, и по sent_on она бы не попала в
		// выборку
		return this.messages.reduce(
			(latest, message) => (message.creation > latest ? message.creation : latest),
			""
		);
	}

	append_message(message) {
		if (!message || message.chat !== this.chat) return;

		if (this.messages.some((existing) => existing.name === message.name)) {
			// То же сообщение могло прийти и событием, и поллингом
			return;
		}

		const el = this.$messages[0];
		const stick = this.at_bottom();
		const scroll_top = el.scrollTop;

		this.messages.push(message);
		this.render_messages();

		// Перерисовка сбрасывает прокрутку в ноль; новое сообщение уходит вниз
		// и ничего выше не сдвигает, поэтому позицию можно просто вернуть
		el.scrollTop = stick ? el.scrollHeight : scroll_top;
	}

	render_conversation() {
		if (!this.chat) {
			this.$header.empty();
			this.$composer.empty();
			this.$messages.html(`<div class="tg-empty">${__("Select a chat to start")}</div>`);
			return;
		}

		const chat = this.current_chat();

		this.$header.html(`
			<div class="tg-header-title">${frappe.utils.escape_html(
				(chat && chat.title) || this.chat
			)}</div>
			<div class="tg-header-meta">
				<span>${frappe.utils.escape_html((chat && chat.type) || "")}</span>
				<a href="/app/telegram-chat/${encodeURIComponent(this.chat)}">${__(
					"Open chat"
				)}</a>
			</div>
		`);

		this.render_composer();
	}

	render_composer() {
		if (!this.can_write) {
			this.$composer.html(
				`<div class="tg-readonly">${__(
					"You do not have permission to send messages"
				)}</div>`
			);
			return;
		}

		this.$composer.html(`
			<select class="form-control tg-format" title="${__("Formatting")}">
				<option value="Plain text">${__("Plain text")}</option>
				<option value="HTML">HTML</option>
				<option value="MarkdownV2">MarkdownV2</option>
			</select>
			<textarea class="form-control tg-input" rows="1"
				placeholder="${__("Write a message...")}"></textarea>
			<button class="btn btn-primary tg-send">${__("Send")}</button>
		`);

		this.$input = this.$composer.find(".tg-input");
		this.$format = this.$composer.find(".tg-format");
		this.$send = this.$composer.find(".tg-send");
	}

	render_messages() {
		if (!this.messages.length) {
			this.$messages.html(`<div class="tg-empty">${__("No messages yet")}</div>`);
			return;
		}

		const html = [];

		if (this.has_more) {
			html.push(`
				<div class="tg-load-more-wrapper">
					<button class="btn btn-xs btn-default tg-load-more">${__(
						"Load earlier messages"
					)}</button>
				</div>
			`);
		}

		let day = null;

		this.messages.forEach((message) => {
			const message_day = this.day_key(message.timestamp);

			if (message_day !== day) {
				day = message_day;
				html.push(`<div class="tg-day">${this.day_label(message.timestamp)}</div>`);
			}

			html.push(this.message_html(message));
		});

		this.$messages.html(html.join(""));
	}

	message_html(message) {
		const outgoing = message.direction === "Outgoing";
		const marks = [];

		if (message.is_edited) marks.push(__("edited"));
		if (message.is_deleted) marks.push(__("deleted"));

		const sender =
			!outgoing && message.sender
				? `<div class="tg-sender">${frappe.utils.escape_html(message.sender)}</div>`
				: "";

		return `
			<div class="tg-message ${outgoing ? "tg-outgoing" : "tg-incoming"}">
				<div class="tg-bubble ${message.is_deleted ? "tg-deleted" : ""}">
					${sender}
					<div class="tg-text">${frappe.utils.escape_html(message.content)}</div>
					<div class="tg-meta">
						${marks.length ? `<span class="tg-mark">${marks.join(", ")}</span>` : ""}
						<span class="tg-time">${this.short_time(message.timestamp, true)}</span>
					</div>
				</div>
			</div>
		`;
	}

	// -- отправка -----------------------------------------------------------

	send() {
		if (this.sending || !this.chat || !this.$input) return;

		const content = this.$input.val();
		if (!content.trim()) return;

		this.sending = true;
		this.$send.prop("disabled", true);

		frappe
			.call({
				method: "habibi_telegram.api.send_chat_message",
				args: {
					chat_id: this.chat,
					content: content,
					text_format: this.$format.val(),
				},
			})
			.then((r) => {
				if (!r.message) return;

				// Текст очищаем только после успеха: не набирать же его заново,
				// если Telegram отказал
				this.$input.val("").trigger("input");
				this.append_message(r.message);
				this.scroll_to_bottom();
				this.load_chats({ silent: true });
			})
			.always(() => {
				this.sending = false;
				this.$send.prop("disabled", false);
				this.focus_input();
			});
	}

	// -- мелочи -------------------------------------------------------------

	at_bottom() {
		const el = this.$messages[0];

		return el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_TO_BOTTOM_PX;
	}

	scroll_to_bottom() {
		const el = this.$messages[0];
		el.scrollTop = el.scrollHeight;
	}

	focus_input() {
		this.$input && this.$input.focus();
	}

	autosize(textarea) {
		textarea.style.height = "auto";
		textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
	}

	initials(title) {
		return (title || "?")
			.trim()
			.split(/\s+/)
			.slice(0, 2)
			.map((word) => word[0])
			.join("")
			.toUpperCase();
	}

	day_key(timestamp) {
		// Дату берём в часовом поясе пользователя — по ней же рисуется
		// разделитель дня
		return timestamp ? frappe.datetime.convert_to_user_tz(timestamp).split(" ")[0] : "";
	}

	short_time(timestamp, always_time = false) {
		if (!timestamp) return "";

		const local = moment(frappe.datetime.convert_to_user_tz(timestamp));

		if (always_time || local.isSame(moment(), "day")) {
			return local.format("HH:mm");
		}

		return local.format("DD.MM.YY");
	}

	day_label(timestamp) {
		if (!timestamp) return "";

		const local = moment(frappe.datetime.convert_to_user_tz(timestamp));

		if (local.isSame(moment(), "day")) return __("Today");
		if (local.isSame(moment().subtract(1, "day"), "day")) return __("Yesterday");

		return frappe.datetime.global_date_format(local.format("YYYY-MM-DD"));
	}
};
