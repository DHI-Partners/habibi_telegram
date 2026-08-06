"""
Подготовка голосовых сообщений.

Голосовым Telegram считает не всякий звук: нужен OGG/OPUS (ещё принимаются MP3
и M4A), иначе запись приедет в чат обычным файлом. Firefox пишет ровно так, а
Chrome кладёт тот же Opus в контейнер WebM — и один этот факт решает, покажет
Telegram пузырёк с волной или вложение.

Перекодировать нечего: и там, и там внутри лежат одни и те же пакеты Opus.
Поэтому здесь не перекодировщик, а перекладка пакетов из одного контейнера в
другой — на чистом Python, без ffmpeg, которого в образе Frappe нет.

Форматы, которые понимает MediaRecorder в браузерах:

    Firefox   audio/ogg;codecs=opus    уходит как есть
    Chrome    audio/webm;codecs=opus   перекладываем в Ogg
    Safari    audio/mp4                уходит как есть, Telegram знает M4A
"""

import frappe
from frappe import _

# Больше этого не принимаем: голосовое такого размера — это уже не голосовое,
# а у Telegram всё равно свой потолок
MAX_VOICE_BYTES = 20 * 1024 * 1024

# Opus всегда считает время в 48 кГц, независимо от частоты записи
OPUS_SAMPLE_RATE = 48000

# -- EBML/Matroska ------------------------------------------------------------

EBML_SEGMENT = 0x18538067
EBML_TRACKS = 0x1654AE6B
EBML_TRACK_ENTRY = 0xAE
EBML_TRACK_NUMBER = 0xD7
EBML_CODEC_ID = 0x86
EBML_CODEC_PRIVATE = 0x63A2
EBML_CLUSTER = 0x1F43B675
EBML_BLOCK_GROUP = 0xA0
EBML_SIMPLE_BLOCK = 0xA3
EBML_BLOCK = 0xA1

# В эти элементы заходим внутрь, остальные пропускаем по длине
EBML_MASTERS = (EBML_SEGMENT, EBML_TRACKS, EBML_CLUSTER, EBML_BLOCK_GROUP)


class UnsupportedAudio(Exception):
	"""Запись не в том виде, в каком её можно отправить голосовым."""


def prepare_voice(content: bytes, filename: str = None) -> frappe._dict:
	"""
	Привести запись к тому, что Telegram примет как голосовое.

	Возвращает content / filename / mime / duration; duration в секундах и
	только для Ogg — там он считается по самой записи, а не со слов браузера.
	"""
	if not content:
		frappe.throw(_("Recording is empty"))

	if len(content) > MAX_VOICE_BYTES:
		frappe.throw(
			_("Recording is too large: {0} MB, limit is {1} MB").format(
				round(len(content) / 1024 / 1024, 1), MAX_VOICE_BYTES // 1024 // 1024
			)
		)

	kind = probe(content)

	if kind == "webm":
		try:
			content = webm_to_ogg(content)
		except UnsupportedAudio as e:
			frappe.throw(
				_("Could not convert the recording to a voice message: {0}").format(str(e))
			)
		kind = "ogg"

	if kind == "ogg":
		return frappe._dict(
			content=content,
			filename=_named(filename, "ogg"),
			mime="audio/ogg",
			duration=ogg_duration(content),
		)

	if kind == "mp4":
		# M4A Telegram принимает голосовым сам; длительность лежит в moov,
		# читать его ради одной цифры не стоит
		return frappe._dict(
			content=content, filename=_named(filename, "m4a"), mime="audio/mp4", duration=None
		)

	frappe.throw(_("Unsupported audio format. Record in OGG/Opus, WebM/Opus or M4A."))


def probe(content: bytes) -> str:
	"""ogg / webm / mp4 — по сигнатуре в начале файла."""
	if content[:4] == b"OggS":
		return "ogg"

	if content[:4] == b"\x1a\x45\xdf\xa3":
		return "webm"

	if content[4:8] == b"ftyp":
		return "mp4"

	return ""


def _named(filename: str, extension: str) -> str:
	base = (filename or "voice").rsplit("/", 1)[-1].rsplit(".", 1)[0] or "voice"

	return f"{base}.{extension}"


# -- WebM → Ogg ---------------------------------------------------------------


def webm_to_ogg(data: bytes) -> bytes:
	"""Переложить пакеты Opus из контейнера WebM в контейнер Ogg."""
	head, packets = _parse_webm(data)

	if not head.startswith(b"OpusHead"):
		raise UnsupportedAudio(_("the recording is not Opus"))

	if not packets:
		raise UnsupportedAudio(_("the recording has no audio"))

	return _write_ogg(head, packets)


def _parse_webm(data: bytes) -> tuple[bytes, list]:
	tracks = []
	blocks = []
	_scan(data, 0, len(data), tracks, blocks)

	opus = next((t for t in tracks if t.get("codec") == "A_OPUS"), None)
	if not opus:
		raise UnsupportedAudio(_("no Opus track in the recording"))

	packets = [frame for number, frame in blocks if number == opus.get("number")]

	return opus.get("head") or b"", packets


def _scan(data: bytes, pos: int, end: int, tracks: list, blocks: list) -> int:
	"""
	Пройти элементы EBML, собирая дорожки и блоки.

	Дерево не строим: интересного здесь всего два вида элементов, а плоский
	обход заодно снимает вопрос с элементами неизвестной длины — MediaRecorder
	пишет запись на ходу и длину Segment с Cluster не знает.
	"""
	while pos < end:
		element, pos = _read_id(data, pos, end)
		size, pos = _read_size(data, pos, end)

		if element in EBML_MASTERS:
			child_end = end if size is None else min(pos + size, end)
			pos = _scan(data, pos, child_end, tracks, blocks)
			continue

		if size is None:
			raise UnsupportedAudio(_("unexpected element of unknown length"))

		payload = data[pos : pos + size]
		pos += size

		if element == EBML_TRACK_ENTRY:
			tracks.append(_read_track(payload))
		elif element in (EBML_SIMPLE_BLOCK, EBML_BLOCK):
			blocks.extend(_read_block(payload))

	return pos


def _read_track(payload: bytes) -> dict:
	track = {}
	pos = 0
	end = len(payload)

	while pos < end:
		element, pos = _read_id(payload, pos, end)
		size, pos = _read_size(payload, pos, end)
		if size is None:
			raise UnsupportedAudio(_("unexpected element of unknown length"))

		value = payload[pos : pos + size]
		pos += size

		if element == EBML_TRACK_NUMBER:
			track["number"] = int.from_bytes(value, "big")
		elif element == EBML_CODEC_ID:
			track["codec"] = value.rstrip(b"\x00").decode("ascii", "ignore")
		elif element == EBML_CODEC_PRIVATE:
			track["head"] = value

	return track


def _read_block(payload: bytes) -> list:
	"""(номер дорожки, пакет) — из SimpleBlock или Block."""
	number, pos = _read_size(payload, 0, len(payload))
	if number is None:
		raise UnsupportedAudio(_("malformed block"))

	# 2 байта времени внутри кластера + байт флагов
	pos += 2
	flags = payload[pos]
	pos += 1

	lacing = (flags >> 1) & 0x03
	frames = payload[pos:]

	if lacing == 0:
		return [(number, frames)]

	if lacing == 2:
		# Фиксированное разбиение: следующий байт — количество кадров минус один
		count = frames[0] + 1
		frames = frames[1:]
		if len(frames) % count:
			raise UnsupportedAudio(_("malformed fixed lacing"))
		step = len(frames) // count

		return [(number, frames[i * step : (i + 1) * step]) for i in range(count)]

	# Xiph и EBML lacing браузеры не используют — разбирать их незачем
	raise UnsupportedAudio(_("unsupported frame lacing"))


def _read_id(data: bytes, pos: int, end: int) -> tuple[int, int]:
	"""Идентификатор читается вместе со служебными битами: по ним он и опознаётся."""
	length = _vint_length(data, pos, end)

	return int.from_bytes(data[pos : pos + length], "big"), pos + length


def _read_size(data: bytes, pos: int, end: int) -> tuple[int | None, int]:
	"""Длина читается без служебных битов. None — длина неизвестна."""
	length = _vint_length(data, pos, end)

	mask = 0x7F >> (length - 1)
	value = data[pos] & mask
	unknown = value == mask

	for byte in data[pos + 1 : pos + length]:
		value = (value << 8) | byte
		unknown = unknown and byte == 0xFF

	return (None if unknown else value), pos + length


def _vint_length(data: bytes, pos: int, end: int) -> int:
	if pos >= end:
		raise UnsupportedAudio(_("recording ended unexpectedly"))

	first = data[pos]
	for length in range(1, 9):
		if first & (0x80 >> (length - 1)):
			if pos + length > end:
				raise UnsupportedAudio(_("recording ended unexpectedly"))

			return length

	raise UnsupportedAudio(_("malformed EBML"))


# -- Ogg ----------------------------------------------------------------------

# Максимум сегментов на страницу — предел формата
OGG_MAX_SEGMENTS = 255


def _write_ogg(head: bytes, packets: list, serial: int = 1) -> bytes:
	pre_skip = int.from_bytes(head[10:12], "little") if len(head) >= 12 else 0

	pages = [
		_ogg_page(serial, 0, 0x02, 0, [head]),
		_ogg_page(serial, 1, 0x00, 0, [_opus_tags()]),
	]

	sequence = 2
	batch = []
	segments = 0
	granule = pre_skip

	for packet in packets:
		needed = len(packet) // 255 + 1
		if needed > OGG_MAX_SEGMENTS:
			raise UnsupportedAudio(_("audio packet is too large"))

		if batch and segments + needed > OGG_MAX_SEGMENTS:
			pages.append(_ogg_page(serial, sequence, 0x00, granule, batch))
			sequence += 1
			batch, segments = [], 0

		batch.append(packet)
		segments += needed
		# Позиция страницы — сколько отсчётов уже раскодировано к её концу
		granule += _packet_samples(packet)

	pages.append(_ogg_page(serial, sequence, 0x04, granule, batch))

	return b"".join(pages)


def _opus_tags() -> bytes:
	vendor = b"habibi_telegram"

	return b"OpusTags" + len(vendor).to_bytes(4, "little") + vendor + (0).to_bytes(4, "little")


def _ogg_page(serial: int, sequence: int, flags: int, granule: int, packets: list) -> bytes:
	lacing = bytearray()
	body = bytearray()

	for packet in packets:
		remaining = len(packet)
		while remaining >= 255:
			lacing.append(255)
			remaining -= 255
		# Последний сегмент короче 255 — он же и признак конца пакета
		lacing.append(remaining)
		body += packet

	page = bytearray(b"OggS")
	page.append(0)
	page.append(flags)
	page += granule.to_bytes(8, "little", signed=True)
	page += serial.to_bytes(4, "little")
	page += sequence.to_bytes(4, "little")
	page += b"\x00\x00\x00\x00"  # место под контрольную сумму
	page.append(len(lacing))
	page += lacing
	page += body

	page[22:26] = _ogg_crc(page).to_bytes(4, "little")

	return bytes(page)


def _ogg_crc_table() -> list:
	table = []

	for index in range(256):
		value = index << 24
		for _bit in range(8):
			if value & 0x80000000:
				value = ((value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
			else:
				value = (value << 1) & 0xFFFFFFFF
		table.append(value)

	return table


OGG_CRC_TABLE = _ogg_crc_table()


def _ogg_crc(page: bytes) -> int:
	"""У Ogg своя CRC32: без отражения битов и без финального инвертирования."""
	crc = 0

	for byte in page:
		crc = ((crc << 8) & 0xFFFFFFFF) ^ OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]

	return crc


def _packet_samples(packet: bytes) -> int:
	"""
	Сколько отсчётов в пакете — по его первому байту (TOC).

	Длительность кадра задаётся конфигурацией, а их количество — двумя младшими
	битами: см. RFC 6716, раздел 3.1.
	"""
	if not packet:
		return 0

	toc = packet[0]
	config = toc >> 3
	code = toc & 0x03

	if config < 12:
		frame = (480, 960, 1920, 2880)[config & 0x03]
	elif config < 16:
		frame = (480, 960)[config & 0x01]
	else:
		frame = (120, 240, 480, 960)[config & 0x03]

	if code == 0:
		count = 1
	elif code in (1, 2):
		count = 2
	else:
		count = packet[1] & 0x3F if len(packet) > 1 else 1

	return frame * count


def ogg_duration(data: bytes) -> int | None:
	"""Длительность записи в секундах — по позиции последней страницы Ogg."""
	last = data.rfind(b"OggS")
	if last < 0 or last + 14 > len(data):
		return None

	granule = int.from_bytes(data[last + 6 : last + 14], "little", signed=True)
	if granule <= 0:
		return None

	head = data.find(b"OpusHead")
	pre_skip = (
		int.from_bytes(data[head + 10 : head + 12], "little")
		if head >= 0 and head + 12 <= len(data)
		else 0
	)

	return max(1, round((granule - pre_skip) / OPUS_SAMPLE_RATE))
