import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
SITE_URL = os.environ["SITE_URL"].rstrip("/")
API = f"https://api.telegram.org/bot{TOKEN}"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("t-bot-sonar")

HOOKS = [
    "Кажется, тут намечается что-то интересное. Заглянешь? 👀",
    "Поймал сигнал из интернета: на сайте есть кое-что для тебя ✨",
    "Небольшое приключение на сегодня уже ждёт тебя по ссылке ниже 🚀",
]


def api(method, payload):
    request = urllib.request.Request(
        f"{API}/{method}", data=urllib.parse.urlencode(payload).encode()
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}")
    return result["result"]


def send(chat_id, text):
    markup = json.dumps({"inline_keyboard": [[{"text": "Открыть сайт ↗", "url": SITE_URL}]]})
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": markup,
        "disable_web_page_preview": "true",
    })


def handle(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    if text.startswith("/start"):
        send(chat_id, "Привет! Я подкидываю интересные поводы заглянуть на сайт.\n\n"
                      "Нажми кнопку — и погнали 🚀\n\n"
                      "Команды: /go — открыть сайт, /buzz — случайный повод, /help — помощь")
    elif text.startswith("/go"):
        send(chat_id, "Твоя точка приземления готова 🛬")
    elif text.startswith("/buzz"):
        send(chat_id, random.choice(HOOKS))
    elif text.startswith("/help"):
        send(chat_id, "Я отвечаю только на твои сообщения.\n"
                      "/go — перейти на сайт\n/buzz — получить повод заглянуть\n"
                      "/start — начать заново")
    else:
        send(chat_id, "Чтобы открыть сайт, жми кнопку ниже. Или вызови /buzz — будет сюрприз 😉")


def main():
    offset = 0
    log.info("Bot is running")
    while True:
        try:
            updates = api("getUpdates", {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": '["message"]',
            })
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    try:
                        handle(update["message"])
                    except (urllib.error.URLError, KeyError, ValueError, RuntimeError):
                        log.exception("Could not handle update %s", update["update_id"])
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            log.exception("Polling failed; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
