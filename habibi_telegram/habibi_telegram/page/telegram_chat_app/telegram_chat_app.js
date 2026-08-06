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

// Голосовым Telegram считает OGG/OPUS, MP3 и M4A. Firefox пишет ogg, Safari —
// mp4, Chrome умеет только webm, и его на сервере перекладывают в ogg
const VOICE_MIME_TYPES = [
	"audio/ogg;codecs=opus",
	"audio/mp4",
	"audio/webm;codecs=opus",
	"audio/webm",
];

// Дальше это уже не голосовое сообщение, а подкаст
const MAX_VOICE_SECONDS = 300;

// Что написать на кнопке, пока вложение не скачано
const MEDIA_ACTIONS = {
	voice: "Listen",
	audio: "Listen",
	photo: "Show photo",
	sticker: "Show sticker",
	video: "Show video",
	video_note: "Show video",
	animation: "Show animation",
	document: "Download file",
};

const ICON_MIC = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
	stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
	<rect x="9" y="2" width="6" height="12" rx="3"></rect>
	<path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
	<line x1="12" y1="19" x2="12" y2="22"></line>
</svg>`;

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
		this.recording = null;
		this.uploading = false;
		this.draft = "";
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

		this.$messages.on("click", ".tg-load-media", (e) => {
			const $button = $(e.currentTarget);
			this.load_media($button.attr("data-message"), $button);
		});

		this.$messages.on("click", ".tg-image", (e) => preview_image(e.currentTarget.src));

		// Композер перерисовывается вместе с чатом, поэтому слушаем контейнер
		this.$composer.on("click", ".tg-send", () => this.send());
		this.$composer.on("click", ".tg-record", () => this.start_recording());
		this.$composer.on("click", ".tg-rec-send", () => this.finish_recording(true));
		this.$composer.on("click", ".tg-rec-cancel", () => this.finish_recording(false));

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

		// Запись всегда про открытый чат — уходя, бросаем её
		this.finish_recording(false);
		this.draft = "";

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

		const known = this.messages.findIndex((existing) => existing.name === message.name);

		if (known >= 0) {
			// То же сообщение могло прийти и событием, и поллингом. Но второе
			// событие о голосовом несёт ссылку на запись, поэтому не отбрасываем
			// его, а обновляем показанное
			if (JSON.stringify(this.messages[known]) === JSON.stringify(message)) return;
		}

		const el = this.$messages[0];
		const stick = this.at_bottom();
		const scroll_top = el.scrollTop;

		if (known >= 0) {
			this.messages[known] = message;
		} else {
			this.messages.push(message);
		}

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

		// Набранное переживает переключение в запись и обратно
		if (this.$input && this.$input.length) {
			this.draft = this.$input.val();
		}
		this.$input = this.$format = this.$send = null;

		if (this.uploading) {
			this.$composer.html(
				`<div class="tg-readonly">${__("Sending voice message...")}</div>`
			);
			return;
		}

		if (this.recording) {
			this.$composer.html(`
				<div class="tg-recording">
					<span class="tg-rec-dot"></span>
					<span class="tg-rec-time">${this.timer_label()}</span>
					<span class="tg-rec-hint">${__("Recording, up to {0} min", [
						Math.round(MAX_VOICE_SECONDS / 60),
					])}</span>
				</div>
				<button class="btn btn-default tg-rec-cancel">${__("Cancel")}</button>
				<button class="btn btn-primary tg-rec-send">${__("Send")}</button>
			`);
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
			<button class="btn btn-default tg-record" title="${__(
				"Record voice message"
			)}">${ICON_MIC}</button>
			<button class="btn btn-primary tg-send">${__("Send")}</button>
		`);

		this.$input = this.$composer.find(".tg-input");
		this.$format = this.$composer.find(".tg-format");
		this.$send = this.$composer.find(".tg-send");

		if (this.draft) {
			this.$input.val(this.draft).trigger("input");
		}
	}

	// -- голосовые ----------------------------------------------------------

	start_recording() {
		if (this.recording || this.uploading || !this.chat) return;

		if (!navigator.mediaDevices || !window.MediaRecorder) {
			frappe.msgprint(__("This browser cannot record audio"));
			return;
		}

		navigator.mediaDevices
			.getUserMedia({ audio: true })
			.then((stream) => this.open_recorder(stream))
			.catch(() =>
				frappe.msgprint({
					title: __("Microphone unavailable"),
					// Микрофон браузер даёт только в защищённом контексте —
					// https либо localhost, — и только с разрешения человека
					message: __(
						"Allow microphone access for this site. Recording also requires HTTPS or localhost."
					),
					indicator: "red",
				})
			);
	}

	open_recorder(stream) {
		const mime = VOICE_MIME_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
		const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
		const chunks = [];

		recorder.ondataavailable = (event) => {
			if (event.data && event.data.size) chunks.push(event.data);
		};

		this.recording = { recorder, chunks, stream, started: Date.now() };
		recorder.start();

		this.timer = setInterval(() => {
			if (this.recorded_seconds() >= MAX_VOICE_SECONDS) {
				this.finish_recording(true);
				return;
			}

			this.$composer.find(".tg-rec-time").text(this.timer_label());
		}, 500);

		this.render_composer();
	}

	finish_recording(send) {
		if (!this.recording || this.recording.stopping) return;

		const state = this.recording;
		const seconds = this.recorded_seconds();

		state.stopping = true;
		clearInterval(this.timer);

		state.recorder.onstop = () => {
			// Микрофон отпускаем сразу, иначе индикатор записи висит во вкладке
			state.stream.getTracks().forEach((track) => track.stop());
			this.recording = null;
			this.render_composer();

			if (!send) return;

			if (seconds < 1) {
				frappe.show_alert({ message: __("Recording is too short"), indicator: "orange" });
				return;
			}

			this.send_voice(new Blob(state.chunks, { type: state.recorder.mimeType }), seconds);
		};

		state.recorder.stop();
	}

	send_voice(blob, seconds) {
		const form = new FormData();
		form.append("file", blob, `voice.${voice_extension(blob.type)}`);
		form.append("chat_id", this.chat);
		form.append("duration", seconds);

		this.uploading = true;
		this.render_composer();

		// Запись уходит формой, а не через frappe.call: тот шлёт JSON, и звук
		// пришлось бы паковать в base64
		fetch("/api/method/habibi_telegram.api.send_voice_message", {
			method: "POST",
			headers: { "X-Frappe-CSRF-Token": frappe.csrf_token, Accept: "application/json" },
			body: form,
		})
			.then((response) => response.json().then((data) => ({ response, data })))
			.then(({ response, data }) => {
				if (!response.ok) {
					frappe.msgprint({
						title: __("Voice message not sent"),
						message: server_error(data),
						indicator: "red",
					});
					return;
				}

				if (data.message) {
					this.append_message(data.message);
					this.scroll_to_bottom();
					this.load_chats({ silent: true });
				}
			})
			.catch(() =>
				frappe.msgprint({
					title: __("Voice message not sent"),
					message: __("Could not reach the server"),
					indicator: "red",
				})
			)
			.finally(() => {
				this.uploading = false;
				this.render_composer();
				this.focus_input();
			});
	}

	recorded_seconds() {
		return this.recording ? Math.round((Date.now() - this.recording.started) / 1000) : 0;
	}

	timer_label() {
		const total = this.recorded_seconds();

		return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
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

		const media = this.media_html(message);

		// У вложения без подписи весь текст — это пометка вида «[voice]».
		// Рядом с самим вложением она уже ничего не добавляет
		const content =
			media && message.content === `[${message.media_type}]` ? "" : message.content;

		return `
			<div class="tg-message ${outgoing ? "tg-outgoing" : "tg-incoming"}">
				<div class="tg-bubble ${message.is_deleted ? "tg-deleted" : ""}">
					${sender}
					${media}
					<div class="tg-text">${frappe.utils.escape_html(content)}</div>
					<div class="tg-meta">
						${marks.length ? `<span class="tg-mark">${marks.join(", ")}</span>` : ""}
						<span class="tg-time">${this.short_time(message.timestamp, true)}</span>
					</div>
				</div>
			</div>
		`;
	}

	media_html(message) {
		const kind = message.media_type;
		if (!kind) return "";

		if (!message.file_url) {
			// Вложения не качаются сами: в живой группе фотографии идут потоком.
			// Пока не попросили — в истории только пометка
			return `<button class="btn btn-xs btn-default tg-load-media"
				data-message="${frappe.utils.escape_html(message.name)}">${__(
					MEDIA_ACTIONS[kind] || "Download attachment"
				)}</button>`;
		}

		const url = frappe.utils.escape_html(message.file_url);

		if (["voice", "audio"].includes(kind)) {
			return `<audio class="tg-audio" controls preload="none" src="${url}"></audio>`;
		}

		if (["photo", "sticker"].includes(kind)) {
			// Открывается окном поверх переписки, а не новой вкладкой: уходить
			// со страницы ради одной картинки незачем
			return `<img class="tg-image" src="${url}" loading="lazy">`;
		}

		if (["video", "video_note", "animation"].includes(kind)) {
			return `<video class="tg-video" controls preload="metadata" src="${url}"></video>`;
		}

		return `<a class="tg-file" href="${url}" target="_blank" rel="noopener" download>${
			frappe.utils.escape_html(message.file_name || __("File"))
		}</a>`;
	}

	load_media(name, $button) {
		$button.prop("disabled", true).text(__("Loading..."));

		frappe
			.call({ method: "habibi_telegram.api.download_media", args: { message: name } })
			.then((r) => {
				if (r.message) this.append_message(r.message);
			})
			.fail(() => {
				// Текст ошибки frappe.call показал сам; кнопку возвращаем в строй
				$button.prop("disabled", false).text(__("Retry"));
			});
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
				this.draft = "";
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

function preview_image(url) {
	// Окно вешаем на body, а не внутрь страницы: у консоли своя прокрутка, и
	// внутри неё картинка во весь экран не раскроется
	const $lightbox = $(`
		<div class="tg-lightbox">
			<img src="${frappe.utils.escape_html(url)}">
			<a class="tg-lightbox-open" href="${frappe.utils.escape_html(url)}"
				target="_blank" rel="noopener">${__("Open original")}</a>
			<button class="tg-lightbox-close" aria-label="${__("Close")}">&times;</button>
		</div>
	`).appendTo(document.body);

	const close = () => {
		$lightbox.remove();
		$(document).off("keydown.tg-lightbox");
	};

	$lightbox.on("click", (e) => {
		// Ссылка «открыть оригинал» — единственное, что не закрывает окно
		if (!$(e.target).hasClass("tg-lightbox-open")) close();
	});

	$(document).on("keydown.tg-lightbox", (e) => {
		if (e.key === "Escape") close();
	});
}

function voice_extension(mime) {
	if (!mime) return "webm";
	if (mime.includes("ogg")) return "ogg";
	if (mime.includes("mp4")) return "m4a";

	return "webm";
}

function server_error(data) {
	// Запись уходит мимо frappe.call, поэтому текст ошибки достаём сами
	try {
		const messages = JSON.parse(data._server_messages || "[]").map(
			(message) => JSON.parse(message).message
		);

		if (messages.length) return messages.join("<br>");
	} catch (e) {
		// Ответ не в том виде, в каком его ждут, — покажем что есть
	}

	return data.exception || data.exc_type || __("Unknown error");
}
