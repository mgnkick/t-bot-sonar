import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from digest import canonical_url, collect_ranked, make_digest, translate_items_to_russian

TOKEN = os.environ["BOT_TOKEN"]
SITE_URL = "https://wifisonar.ru"
FREEDOM_URL = os.getenv("FREEDOM_URL", "https://freedom1.ru/").rstrip("/")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@wifisonar")
API = f"https://api.telegram.org/bot{TOKEN}"
UTC_PLUS_3 = timezone(timedelta(hours=3), "UTC+03:00")
STATE_DIR = Path(os.getenv("STATE_DIR", "/var/lib/t-bot-sonar"))
STATE_FILE = STATE_DIR / "digest-state.json"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("t-bot-sonar")

HOOKS = [
    "Кажется, тут намечается что-то интересное. Заглянешь? 👀",
    "Поймал сигнал из интернета: на сайте есть кое-что для тебя ✨",
    "Небольшое приключение на сегодня уже ждёт тебя по ссылке ниже 🚀",
]
SCHEDULE = ((8, "утро"), (13, "день"), (18, "вечер"))
GUIDE_ROOT = "/guides/"
REGIONS = (
    ("Краснодарский край", r"краснодар(?:ский край|ского края|ском крае)?|кубан[ьи]|соч[ьи]|анап[аыу]|новороссийск[а-я]*|армавир[а-я]*"),
    ("Республика Адыгея", r"адыге[яие]|адыгейск[а-я]*|майкоп[а-я]*"),
    ("Республика Башкортостан", r"башкортостан[а-я]*|башкири[яие]|уф[аыеу]|стерлитамак[а-я]*|салават[а-я]*"),
    ("Челябинская область", r"челябинск[а-я]*|магнитогорск[а-я]*|озёрск[а-я]*|озерск[а-я]*|миасс[а-я]*|златоуст[а-я]*"),
)
PROVIDER_INTENT = re.compile(
    r"\b(интернет|провайдер\w*|provider\w*|подключ\w*|тариф\w*|"
    r"домашн\w*\s+интернет\w*|wi[ -]?fi|вайфай|связ\w*)\b",
    re.IGNORECASE,
)


def api(method, payload):
    request = urllib.request.Request(
        f"{API}/{method}", data=urllib.parse.urlencode(payload).encode()
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}")
    return result["result"]


def tracked_site_url(path, campaign, content="landing"):
    base = SITE_URL.rstrip("/") + "/" + path.lstrip("/")
    parts = urllib.parse.urlsplit(base)
    params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    params.update({
        "utm_source": "telegram",
        "utm_medium": "channel",
        "utm_campaign": campaign,
        "utm_content": content,
    })
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(params), parts.fragment))


DONATE_BTC_ADDRESS = os.getenv("DONATE_BTC_ADDRESS") or "bc1qy3772x4qs82adktgvp9ys7pgaurq0tj38kvzt8"
DONATE_RVN_ADDRESS = os.getenv("DONATE_RVN_ADDRESS") or "RMW9R8bip7vUmMQV2xo8hSQuojgMFmfGW9"
DONATE_SBER_CARD = os.getenv("DONATE_SBER_CARD") or "5336690304351846"
DONATE_ETH_ADDRESS = os.getenv("DONATE_ETH_ADDRESS", "").strip()
if not re.fullmatch(r"0x[a-fA-F0-9]{40}", DONATE_ETH_ADDRESS):
    log.warning("ETH donation address is missing or invalid; hiding ETH option")


def button(include_channel=False, campaign="bot_menu", site_path=GUIDE_ROOT):
    keyboard = [
        [{"text": "Сайт приложения ↗", "url": tracked_site_url("/", campaign, "app_page")}],
        [{"text": "Фридом. Интернет друзей. ↗", "url": FREEDOM_URL}],
    ]
    if include_channel:
        keyboard.append([{"text": "Подписаться на канал ↗", "url": "https://t.me/wifisonar"}])
        keyboard.append([{"text": "Поддержать WiFi Sonar ❤️", "callback_data": "donate"}])
    return json.dumps({"inline_keyboard": keyboard})


def donation_keyboard():
    return json.dumps({"inline_keyboard": [
        [{"text": "Криптовалюта", "callback_data": "donate_crypto"}],
        [{"text": "Сбербанк · RUB", "callback_data": "donate_sber"}],
        [{"text": "Назад", "callback_data": "donate_back"}],
    ]})


def crypto_keyboard():
    coins = [
        ("BTC", DONATE_BTC_ADDRESS),
        ("ETH", DONATE_ETH_ADDRESS),
        ("RVN", DONATE_RVN_ADDRESS),
    ]
    buttons = [[{"text": symbol, "callback_data": f"donate_coin_{symbol.lower()}"}]
               for symbol, address in coins if address]
    buttons.append([{"text": "Назад", "callback_data": "donate"}])
    return json.dumps({"inline_keyboard": buttons})


def donation_text():
    methods = []
    if any((DONATE_BTC_ADDRESS, DONATE_ETH_ADDRESS, DONATE_RVN_ADDRESS)):
        methods.append("криптовалютой")
    if DONATE_SBER_CARD:
        methods.append("на карту Сбера в рублях")
    if not methods:
        return ("Спасибо, что хотите поддержать развитие WiFi Sonar! ❤️\\n\\n"
                "Реквизиты пока настраиваются. Загляните немного позже.")
    return ("Спасибо за интерес к поддержке WiFi Sonar! ❤️\\n\\n"
            "Можно поддержать проект " + " или ".join(methods) + ".\\n"
            "Выберите удобный способ ниже.")


def send_donation(chat_id, text, reply_markup):
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
        "disable_web_page_preview": "true",
    })


def send(chat_id, text, campaign="bot_message", site_path=GUIDE_ROOT):
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": button(include_channel=True, campaign=campaign, site_path=site_path),
        "disable_web_page_preview": "true",
    })



def handle_callback(callback):
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not chat_id or not message_id:
        api("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Не удалось открыть меню."})
        return

    if data in ("donate", "donate_back"):
        api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": donation_text(),
            "reply_markup": donation_keyboard(),
        })
    elif data == "donate_crypto":
        if not any((DONATE_BTC_ADDRESS, DONATE_ETH_ADDRESS, DONATE_RVN_ADDRESS)):
            api("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Криптореквизиты пока не настроены."})
            return
        api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": "Выберите криптовалюту. Отправляйте средства только в соответствующую сеть:",
            "reply_markup": crypto_keyboard(),
        })
    elif data.startswith("donate_coin_"):
        coin = data.removeprefix("donate_coin_").upper()
        if coin not in ("BTC", "ETH", "RVN"):
            api("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Неизвестная валюта."})
            return
        address = {"BTC": DONATE_BTC_ADDRESS, "ETH": DONATE_ETH_ADDRESS, "RVN": DONATE_RVN_ADDRESS}[coin]
        if not address:
            api("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Этот кошелёк пока не настроен."})
            return
        network_names = {"BTC": "Bitcoin", "ETH": "Ethereum", "RVN": "Ravencoin"}
        api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": (f"❤️ Поддержать WiFi Sonar · {coin}\\n\\n"
                     f"Сеть: {network_names[coin]}\\n"
                     f"Адрес кошелька:\\n<code>{escape(address)}</code>\\n\\n"
                     "Перед отправкой обязательно сверьте сеть и адрес. Перевод в другую сеть может привести к потере средств."),
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": [
                [{"text": "Назад к выбору криптовалюты", "callback_data": "donate_crypto"}],
            ]}),
        })
    elif data == "donate_sber":
        card = DONATE_SBER_CARD
        recipient = os.getenv("DONATE_SBER_RECIPIENT", "").strip()
        if not card:
            api("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Реквизиты Сбера пока не настроены."})
            return
        text = "❤️ Поддержать WiFi Sonar · Сбербанк\\n\\n"
        text += f"Карта: <code>{escape(card)}</code>\\n"
        if recipient:
            text += f"Получатель: {escape(recipient)}\\n"
        text += "\\nПеревод в рублях. Спасибо за поддержку!"
        api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": [
                [{"text": "Назад к способам поддержки", "callback_data": "donate"}],
            ]}),
        })

    api("answerCallbackQuery", {"callback_query_id": callback["id"]})


def handle(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text.startswith("/") else ""
    if command == "/start":
        send(chat_id, "Привет! В @wifisonar выходят утренний дайджест, дневные советы по Wi‑Fi и вечерние разборы.\n"
                      "Нажми «Подписаться на канал», чтобы получать новые материалы.\n"
                      "Команда /app — открыть сайт приложения.\n"
                      "Команда /donate — поддержать развитие проекта.\n"
                      "Команда /recommend — пример «Фридом» по теме или региону.\n\n"
                      "Публикации выходят в 08:00, 13:00 и 18:00 по UTC+3.\n\n"
                      "Команды: /go — сайт приложения, /buzz — повод заглянуть, /donate — поддержать проект, /help — помощь",
             campaign="wifi_sonar_bot_start")
    elif command == "/go":
        send(chat_id, "Твоя точка приземления готова 🛬", campaign="wifi_sonar_bot_go")
    elif command == "/app":
        send(chat_id, "WiFi Sonar помогает измерять сигнал, сравнивать каналы, проверять DNS, шлюз и задержку, а также отмечать покрытие на плане помещения.\n\n"
                      "На сайте — практические инструкции, описание разрешений и APK для Android 8.0 и новее. Доступность показаний зависит от устройства и версии Android.",
             campaign="wifi_sonar_bot_app_command")
    elif command == "/donate":
        send_donation(chat_id, donation_text(), donation_keyboard())
    elif command == "/buzz":
        send(chat_id, random.choice(HOOKS), campaign="wifi_sonar_bot_buzz")
    elif command == "/help":
        send(chat_id, "В канале три формата: утренний дайджест, дневной практический совет и вечерний разбор.\n"
                      "/recommend [тема или регион] — пример на сайте Фридом\n"
                      "/app — сайт приложения\n"
                      "/subscribe — ссылка на канал с выпусками\n"
                      "/donate — поддержать проект\n"
                      "/go — перейти на сайт\n/buzz — получить повод заглянуть\n"
                      "/start — начать заново", campaign="wifi_sonar_bot_help")
    elif command == "/recommend":
        topic = text.partition(" ")[2]
        send(chat_id, recommend_text(topic), campaign="wifi_sonar_bot_recommend")
    elif command == "/subscribe":
        send(chat_id, "Чтобы быть в курсе новых подборок, подпишитесь на @wifisonar: https://t.me/wifisonar",
             campaign="wifi_sonar_bot_subscribe")
    elif should_refer_user(text):
        region = matched_region(text)
        send(chat_id, recommend_text(region), campaign="wifi_sonar_bot_region_referral")
    else:
        send(chat_id, "Открой подборку в канале, вызови /recommend или жми кнопку, чтобы перейти на сайт 😉",
             campaign="wifi_sonar_bot_reply")


def main():
    offset = 0
    try:
        api("setMyCommands", {"commands": json.dumps([
            {"command": "start", "description": "Начать работу с ботом"},
            {"command": "app", "description": "Сайт приложения"},
            {"command": "donate", "description": "Поддержать WiFi Sonar"},
            {"command": "recommend", "description": "Фридом по теме или региону"},
            {"command": "subscribe", "description": "Канал с подборками"},
            {"command": "go", "description": "Открыть основной сайт"},
            {"command": "help", "description": "Список команд"},
        ], ensure_ascii=False)})
    except (urllib.error.URLError, TimeoutError, RuntimeError):
        log.exception("Could not update Telegram command menu")
    log.info("Bot is running; content scheduled for 08:00, 13:00, and 18:00 UTC+3 in %s", CHANNEL_ID)
    while True:
        try:
            updates = api("getUpdates", {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": '["message", "callback_query"]',
            })
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    try:
                        handle(update["message"])
                    except (urllib.error.URLError, KeyError, ValueError, RuntimeError):
                        log.exception("Could not handle update %s", update["update_id"])
                elif "callback_query" in update:
                    try:
                        handle_callback(update["callback_query"])
                    except (urllib.error.URLError, KeyError, ValueError, RuntimeError):
                        log.exception("Could not handle callback %s", update["update_id"])
            maybe_publish_content()
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            log.exception("Polling failed; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
