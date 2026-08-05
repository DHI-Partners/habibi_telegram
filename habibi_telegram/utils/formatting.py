"""
Telegram понимает только урезанное подмножество HTML, и на любой посторонний тег
отвечает ошибкой вместо отправки сообщения. Поэтому текст, собранный из шаблонов
Frappe (а там легко заезжает <div>, <p>, <br>), приходится чистить.

https://core.telegram.org/bots/api#html-style
"""

import re

SUPPORTED_TAGS = (
	"b",
	"strong",  # жирный
	"i",
	"em",  # курсив
	"u",
	"ins",  # подчёркнутый
	"s",
	"strike",
	"del",  # зачёркнутый
	"a",  # ссылки
	"code",
	"pre",  # код
	"blockquote",
	"tg-spoiler",
)

_ALLOWED_TAG_RE = re.compile(
	r"</?(?:{})(?:\s[^<>]*)?>".format("|".join(SUPPORTED_TAGS)),
	re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


def strip_unsupported_html_tags(txt: str) -> str:
	"""
	Оставляет поддерживаемые теги как есть, всё остальное экранирует.

	Порядок именно такой — сначала прячем разрешённые теги за плейсхолдеры,
	потом экранируем текст целиком, потом возвращаем теги. Если сначала
	экранировать, а потом восстанавливать теги регуляркой, ломаются ссылки
	с амперсандом в query-параметрах.
	"""
	if not txt:
		return txt

	stash = []

	def _hide(match):
		stash.append(match.group(0))
		return f"\x00{len(stash) - 1}\x00"

	txt = _ALLOWED_TAG_RE.sub(_hide, txt)
	txt = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
	txt = _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], txt)

	return txt
