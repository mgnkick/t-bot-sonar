import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from digest import canonical_url, collect_ranked, make_digest

TOKEN = os.environ["BOT_TOKEN"]
SITE_URL = os.environ["SITE_URL"].rstrip("/")
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
SCHEDULE = ((8, "утро"), (18, "вечер"))


def api(method, payload):
    request = urllib.request.Request(
        f"{API}/{method}", data=urllib.parse.urlencode(payload).encode()
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}")
    return result["result"]


def button():
    return json.dumps({"inline_keyboard": [[
        {"text": "Открыть сайт ↗", "url": SITE_URL}
    ]]})


def send(chat_id, text):
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": button(),
        "disable_web_page_preview": "true",
    })


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {"sent": {}, "attempts": {}, "recent": []}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temp_file.replace(STATE_FILE)


def choose_items(ranked, state, now):
    cutoff = now - timedelta(days=2)
    recent = set()
    retained = []
    for entry in state.get("recent", []):
        try:
            seen_at = datetime.fromisoformat(entry["at"])
            if seen_at >= cutoff:
                recent.add(canonical_url(entry["url"]))
                retained.append(entry)
        except (KeyError, TypeError, ValueError):
            continue
    state["recent"] = retained
    fresh = [item for item in ranked if canonical_url(item["url"]) not in recent]
    selected = fresh[:3]
    if len(selected) < 3:
        selected.extend(item for item in ranked if item not in selected and canonical_url(item["url"]) not in {canonical_url(s["url"]) for s in selected})
    return selected[:3]


def maybe_publish_digest():
    if not CHANNEL_ID:
        return
    now = datetime.now(UTC_PLUS_3)
    for hour, slot_name in SCHEDULE:
        slot_start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now < slot_start or now >= slot_start + timedelta(hours=3):
            continue

        slot_key = f"{now.date().isoformat()}:{slot_name}"
        state = load_state()
        if slot_key in state.setdefault("sent", {}):
            continue
        last_attempt = state.setdefault("attempts", {}).get(slot_key)
        if last_attempt:
            try:
                if now - datetime.fromisoformat(last_attempt) < timedelta(minutes=30):
                    continue
            except ValueError:
                pass
        state["attempts"][slot_key] = now.isoformat()
        save_state(state)

        try:
            ranked = collect_ranked()
            items = choose_items(ranked, state, now)
            if len(items) < 3:
                log.warning("Only %d eligible articles for %s digest; will retry", len(items), slot_name)
                return
            label = f"{slot_name}, {slot_start.strftime('%H:%M')} · UTC+3"
            message = make_digest(items, label, escape(SITE_URL, quote=True))
            api("sendMessage", {
                "chat_id": CHANNEL_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "reply_markup": button(),
            })
            state["sent"][slot_key] = now.isoformat()
            state["recent"].extend({"url": canonical_url(item["url"]), "at": now.isoformat()} for item in items)
            save_state(state)
            log.info("Published %s digest with %d articles", slot_name, len(items))
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, KeyError):
            log.exception("Could not publish %s digest", slot_name)


def handle(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text.startswith("/") else ""
    if command == "/start":
        send(chat_id, "Привет! Дважды в день я собираю популярные материалы о сетях и интернете.\n\n"
                      "Выпуски выходят в 08:00 и 18:00 по UTC+3.\n\n"
                      "Команды: /go — открыть сайт, /buzz — случайный повод, /help — помощь")
    elif command == "/go":
        send(chat_id, "Твоя точка приземления готова 🛬")
    elif command == "/buzz":
        send(chat_id, random.choice(HOOKS))
    elif command == "/help":
        send(chat_id, "Подборки публикуются в канале дважды в день.\n"
                      "/go — перейти на сайт\n/buzz — получить повод заглянуть\n"
                      "/start — начать заново")
    else:
        send(chat_id, "Открой подборку в канале или жми кнопку ниже, чтобы перейти на сайт 😉")


def main():
    offset = 0
    log.info("Bot is running; digests scheduled for 08:00 and 18:00 UTC+3 in %s", CHANNEL_ID)
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
            maybe_publish_digest()
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            log.exception("Polling failed; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
