# Habibi Telegram

Телеграм-боты для Frappe: уведомления из ERP в чат, входящие сообщения,
привязка телеграм-аккаунта к пользователю Frappe.

Приложение написано на вебхуках и на голом Bot API. Внешних зависимостей нет:
`requests` приходит вместе с frappe, а Bot API — это обычный HTTPS с JSON.

## Почему не python-telegram-bot

Frappe v16 требует Python 3.14. `python-telegram-bot` 13.x, на котором построены
существующие интеграции, туда не ставится: он импортирует `imghdr`, удалённый из
stdlib в 3.13, вендорит urllib3 с `six.moves`, сломанным с 3.12, и тянет
`APScheduler==3.6.3`, которому нужен `pkg_resources`. Версии 20+ решают это ценой
полностью асинхронного API и удалённого `Dispatcher`.

Заодно ушла архитектурная проблема: `Updater` — это долгоживущий процесс с
polling'ом, которому в контейнерном стеке негде жить. Вебхук — обычный
whitelisted-метод, он масштабируется вместе с веб-воркерами и ничего не требует
держать запущенным.

## Установка

```bash
bench get-app https://github.com/DHI-Partners/habibi_telegram --branch master
bench --site <site> install-app habibi_telegram
```

## Настройка бота

1. Получить токен у [@BotFather](https://t.me/BotFather).
2. Завести **Telegram Bot**, вставить токен. При сохранении токен проверяется
   через `getMe`, оттуда же подтягивается username.
3. Нажать **Set Webhook**.

Адрес вебхука собирается из `host_name` сайта:

```
https://<site>/api/method/habibi_telegram.api.webhook?bot=<имя бота>
```

Telegram принимает только HTTPS и только публично доступный адрес, так что на
`localhost` вебхук не зарегистрировать — нужен туннель либо прод.

Каждый апдейт подписан секретом: при `setWebhook` мы передаём `secret_token`,
Telegram возвращает его в заголовке `X-Telegram-Bot-Api-Secret-Token`, и запрос
без верной подписи до обработчиков не доходит.

Из консоли то же самое:

```bash
bench --site <site> telegram list-bots
bench --site <site> telegram set-webhook <имя бота>
bench --site <site> telegram webhook-info <имя бота>   # что о вебхуке думает Telegram
bench --site <site> telegram remove-webhook <имя бота>
```

## Отправка сообщений

```python
from habibi_telegram.client import send_message, send_file, ParseMode

send_message("Заказ <b>SO-0042</b> оплачен", parse_mode=ParseMode.HTML,
             user="manager@example.com")

send_file(frappe.get_doc("File", file_name), message="Счёт",
          user="manager@example.com")
```

Получателя можно задать тремя способами: `user` (пользователь Frappe),
`telegram_user` (документ Telegram User) или `chat_id` (например, групповой чат).
Бот берётся из `from_bot`, а если не указан — тот, что помечен как основной.

HTML чистится перед отправкой: Telegram понимает лишь несколько тегов и на любом
постороннем `<div>` отвечает ошибкой вместо отправки.

### Шаблоны

**Telegram Message Template** хранит Jinja-шаблон и переводы по языкам:

```python
from habibi_telegram.client import send_message_from_template

send_message_from_template("order-paid", context={"doc": doc}, lang="ru",
                           user="manager@example.com")
```

## Уведомления Frappe

Приложение добавляет канал **Telegram** в стандартный Notification. В самом
уведомлении появляется поле «Bot to Send From»; пустое — уйдёт от основного бота.
Получатели, у которых нет связанного Telegram User, пропускаются. Отправка идёт
через очередь `short`, поэтому сохранение документа её не ждёт.

## Входящие сообщения

Пока Telegram User не связан с пользователем Frappe, бот предлагает войти или
зарегистрироваться и не пускает дальше. После входа связь сохраняется, и
обработчики выполняются от имени этого пользователя — права Frappe работают как
обычно.

Кнопку «Signup» можно убрать: она скрывается, если в Website Settings включено
`disable_signup`. Пароль, присланный сообщением, замазывается в базе и удаляется
из чата.

Готовые чаты видно на странице **Telegram Chat** в Desk.

## Свои обработчики

```python
# hooks.py вашего приложения
telegram_bot_handler = ["my_app.telegram.setup"]
```

```python
# my_app/telegram.py
def setup(registry, telegram_bot):
	registry.command("orders", show_orders)
	registry.callback_query(open_order, prefix="order:")
	registry.message(echo, group=10)


def show_orders(context):
	orders = frappe.get_all("Sales Order", limit=5, pluck="name")
	context.reply("\n".join(orders) or "Заказов нет")
```

Обработчики одной группы идут в порядке регистрации, группы — по возрастанию
(отрицательные раньше). Аутентификация висит на группе -100. Чтобы оборвать
цепочку, бросьте `StopHandling`.

В `context` лежит всё нужное: `bot` (клиент Bot API), `telegram_bot`,
`telegram_user`, `telegram_chat`, `telegram_message`, `update` (сырой апдейт),
`chat_id`, `text`, а также `reply()`, `answer_callback()` и состояние диалога
через `get_state()` / `set_state()` / `clear_state()`.

Точки расширения:

| Хук | Назначение |
|---|---|
| `telegram_bot_handler` | регистрация обработчиков |
| `telegram_update_pre_processors` | до обработчиков, на каждый апдейт |
| `telegram_update_post_processors` | после обработчиков |
| `telegram_auth_handlers` | своя аутентификация вместо login/signup |
| `telegram_start_handler` | своя реакция на `/start` |

### Пошаговые диалоги

Состояние диалога хранится в Telegram User, а не в памяти процесса — на вебхуках
каждый апдейт приходит отдельным запросом.

```python
from habibi_telegram.utils.conversation import collect_conversation_details

def ask_details(context):
	details = collect_conversation_details(
		key="delivery",
		meta=[
			{"key": "city", "label": "City", "type": "str"},
			{"key": "qty", "label": "Quantity", "type": "int"},
			{"key": "mode", "label": "Mode", "type": "select", "options": "Air\nSea"},
		],
		context=context,
	)
	if not details.get("_is_complete"):
		return

	frappe.get_doc(doctype="Delivery Request", **details).insert()
```

## DocType'ы

| DocType | Назначение |
|---|---|
| Telegram Bot | токен, username, состояние вебхука |
| Telegram User | телеграм-аккаунт, связь с User, состояние диалога |
| Telegram Chat | личные чаты и группы, где есть бот |
| Telegram Message | история входящих и исходящих |
| Telegram Message Template | Jinja-шаблоны с переводами |

## Лицензия

MIT
