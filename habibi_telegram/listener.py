"""
Постоянные соединения MTProto для всех аккаунтов всех сайтов.

`bench telegram listen-all` — один процесс на весь бенч: по потоку на каждый
подключённый аккаунт, раз в минуту список перечитывается. Без него личные
аккаунты получают сообщения раз в минуту через getDifference, а с ним —
сразу, как и боты с вебхуком.

Пока слушатель жив, он раз в HEARTBEAT_EVERY пишет heartbeat в redis, и cron
этот аккаунт пропускает: одна сессия Telethon в двух соединениях — повод для
Telegram её разорвать. Упал слушатель — heartbeat протух, и через минуту
аккаунт снова на cron: медленнее, но без потерь.
"""

import threading
import time

import frappe
from frappe.utils import get_sites

HEARTBEAT_EVERY = 30
HEARTBEAT_TTL = 90
RESCAN_EVERY = 60


def _heartbeat_key(account: str) -> str:
	# Префикс сайта redis-обёртка frappe добавляет сама
	return f"telegram-listener:{account}"


def mark_alive(account: str):
	frappe.cache().set_value(_heartbeat_key(account), 1, expires_in_sec=HEARTBEAT_TTL)


def is_alive(account: str) -> bool:
	# use_local_cache=False — иначе если этот же процесс уже читал или писал
	# этот ключ раньше (например, сам вызвал mark_alive), get_value вернёт то
	# значение из frappe.local.cache и не заметит, что оно протухло в redis
	return bool(frappe.cache().get_value(_heartbeat_key(account), use_local_cache=False))


def should_listen(account: str) -> bool:
	"""Аккаунт всё ещё включён и подключён.

	rollback — чтобы увидеть свежие данные: процесс живёт часами в одной
	транзакции, и в REPEATABLE READ выключенный на форме аккаунт выглядел бы
	включённым вечно.
	"""
	frappe.db.rollback()
	row = frappe.db.get_value("Telegram Account", account, ["enabled", "sync_enabled", "status"], as_dict=True)
	return bool(row and row.enabled and row.sync_enabled and row.status == "Connected")


def run_all():
	threads = {}

	while True:
		for site in get_sites():
			for account in _accounts_to_listen(site):
				key = (site, account)
				if key in threads and threads[key].is_alive():
					continue
				# Упавший поток перезапускается здесь же — не чаще раза в RESCAN_EVERY
				thread = threading.Thread(
					target=_listen_one, args=(site, account), name=f"telegram:{site}:{account}", daemon=True
				)
				thread.start()
				threads[key] = thread

		time.sleep(RESCAN_EVERY)


def _accounts_to_listen(site: str) -> list[str]:
	try:
		frappe.init(site=site)
		frappe.connect()
		if "habibi_telegram" not in frappe.get_installed_apps():
			return []
		return frappe.get_all(
			"Telegram Account",
			filters={"enabled": 1, "sync_enabled": 1, "status": "Connected"},
			pluck="name",
		)
	except Exception as e:
		# Сайт в миграции или без базы — не повод ронять слушателей остальных
		print(f"[telegram listen-all] {site}: {e}", flush=True)
		return []
	finally:
		frappe.destroy()


def _listen_one(site: str, account: str):
	try:
		frappe.init(site=site)
		frappe.connect()

		from habibi_telegram.user_client import listen

		listen(account, forever=True, heartbeat=True)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Telegram listener stopped ({account})", message=frappe.get_traceback())
		frappe.db.commit()
	finally:
		frappe.destroy()
