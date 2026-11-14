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
SITE_URL = os.environ["SITE_URL"].rstrip("/")
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


def button(include_channel=False, campaign="bot_menu", site_path=GUIDE_ROOT):
    keyboard = [
        [{"text": "Инструкция и WiFi Sonar ↗", "url": tracked_site_url(site_path, campaign, "primary_cta")}],
        [{"text": "О приложении на сайте ↗", "url": tracked_site_url("/", campaign, "app_page")}],
        [{"text": "Проверить Фридом ↗", "url": FREEDOM_URL}],
    ]
    if include_channel:
        keyboard.append([{"text": "Подписаться на канал ↗", "url": "https://t.me/wifisonar"}])
    return json.dumps({"inline_keyboard": keyboard})


def send(chat_id, text, campaign="bot_message", site_path=GUIDE_ROOT):
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": button(include_channel=True, campaign=campaign, site_path=site_path),
        "disable_web_page_preview": "true",
    })


def matched_region(text):
    for label, pattern in REGIONS:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return None


def recommend_text(topic=""):
    topic = topic.strip()
    region = matched_region(topic)
    if region:
        return (f"Ищете интернет-провайдера в регионе «{region}»? На сайте «Фридом» можно "
                "проверить покрытие по адресу и посмотреть доступные тарифы. Доступность зависит от дома: "
                f"введите адрес на сайте.\n\n{FREEDOM_URL}/")
    if topic:
        return (f"По теме «{topic}» можно посмотреть публичный пример провайдера «Фридом»: "
                "на сайте есть карта покрытия, проверка доступности по адресу и сведения об оптических линиях. "
                "Это показывает, как провайдер представляет покрытие и подключение клиентам; внутреннюю "
                f"реализацию конкретной технологии сайт не раскрывает.\n\n{FREEDOM_URL}/")
    return ("Практический пример интернет-провайдера «Фридом»: на сайте можно проверить покрытие "
            f"по адресу и посмотреть доступные тарифы.\n\n{FREEDOM_URL}/")


def should_refer_user(text):
    return bool(PROVIDER_INTENT.search(text) and matched_region(text))


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
    selected = fresh[:5]
    if len(selected) < 5:
        selected.extend(item for item in ranked if item not in selected and canonical_url(item["url"]) not in {canonical_url(s["url"]) for s in selected})
    return selected[:5]


DAY_TIPS = (
    {
        "title": "Найдите слабую зону, а не угадывайте",
        "body": "Измерьте сигнал в нескольких комнатах, держа телефон примерно на одной высоте и в одинаковом положении. Сравнение точек покажет, где покрытие проседает.",
        "action": "Отметьте результаты на плане помещения и повторите замер после перестановки роутера.",
        "path": "/guides/signal/",
        "content": "tip_signal",
    },
    {
        "title": "Сильный сигнал не всегда означает быстрый Wi‑Fi",
        "body": "Уровень сигнала показывает качество радиосвязи с точкой доступа. Скорость также зависит от помех, нагрузки канала и линии провайдера.",
        "action": "Сравните уровень сигнала, задержку и потери пакетов отдельно.",
        "path": "/guides/diagnostics/",
        "content": "tip_signal_vs_quality",
    },
    {
        "title": "Сравните диапазоны в нужной комнате",
        "body": "2,4 ГГц часто лучше проходит через стены, а 5 ГГц может дать больше скорости рядом с роутером. Итог зависит от планировки и соседних сетей.",
        "action": "Проверьте, какой диапазон даёт более устойчивое соединение именно в проблемной точке.",
        "path": "/guides/signal/",
        "content": "tip_bands",
    },
    {
        "title": "Перед сменой канала посмотрите на соседние сети",
        "body": "В многоквартирном доме несколько точек доступа могут делить один радиоканал. Сначала оцените загруженность диапазона, затем меняйте настройки роутера.",
        "action": "Сравните соседние сети по каналам и перепроверьте связь после изменения.",
        "path": "/guides/channels/",
        "content": "tip_channels",
    },
    {
        "title": "Проверьте место установки роутера",
        "body": "Закрытый шкаф, угол квартиры и крупные препятствия могут ухудшать покрытие. Одно небольшое перемещение иногда заметно меняет результат.",
        "action": "Сделайте замеры до и после перестановки в тех же точках.",
        "path": "/guides/signal/",
        "content": "tip_router_placement",
    },
)

EVENING_EXPLAINERS = (
    {
        "title": "Wi‑Fi подключён, но сайты не открываются",
        "body": "Проверьте путь по порядку: связь телефона с роутером, доступность локального шлюза, работу DNS и доступ к внешнему узлу. Так проще понять, где именно возникает сбой.",
        "action": "Сравните DNS-проверку и задержку до внешнего узла; отдельные серверы могут блокировать ping.",
        "path": "/guides/diagnostics/",
        "content": "evening_dns",
    },
    {
        "title": "Как понять, виноват ли радиосигнал",
        "body": "Если проблема только в дальней комнате, измерьте сигнал рядом с роутером и в проблемной точке. Если он стабилен, а сайты всё равно тормозят, проверьте DNS, задержку и потери.",
        "action": "Сравните результаты в нескольких точках и в одно и то же время.",
        "path": "/guides/signal/",
        "content": "evening_signal",
    },
    {
        "title": "Как оценить загруженность Wi‑Fi-канала",
        "body": "Посмотрите, какие каналы используют соседние сети и насколько сильно они перекрываются. Автовыбор роутера может помочь, но результат стоит проверить замерами.",
        "action": "Сравните канал и качество соединения до и после изменения настроек.",
        "path": "/guides/channels/",
        "content": "evening_channels",
    },
    {
        "title": "Что записать перед обращением к провайдеру",
        "body": "Зафиксируйте время сбоя, комнату, уровень сигнала, задержку и потери пакетов. Повторите измерение рядом с роутером: это поможет отделить локальную проблему от внешней.",
        "action": "Не публикуйте пароль сети и точные идентификаторы устройств; для диагностики хватит результатов измерений.",
        "path": "/guides/diagnostics/",
        "content": "evening_provider_checklist",
    },
    {
        "title": "Как провести простое обследование квартиры",
        "body": "Выберите постоянные точки измерения: у роутера, за основной стеной и в самой дальней комнате. Запишите результат в каждой точке и сравните покрытие.",
        "action": "Схема помещения и повторные замеры помогут увидеть, изменилось ли покрытие после настройки.",
        "path": "/guides/signal/",
        "content": "evening_survey",
    },
)


def guide_for_item(item):
    terms = set(item.get("terms", []))
    text = (item.get("title", "") + " " + item.get("summary", "")).casefold()
    if terms & {"wifi channel", "wi-fi channel", "wireless channel", "channel congestion"}:
        return "/guides/channels/"
    if terms & {"dns", "tcp", "udp", "vpn", "wireguard", "firewall", "ddos", "bgp", "routing"}:
        return "/guides/diagnostics/"
    if "channel" in text or terms & {"wifi", "wi-fi", "wireless", "mesh network", "access point"}:
        return "/guides/channels/" if "channel" in text else "/guides/signal/"
    return GUIDE_ROOT


def html_text(value):
    return escape(value, quote=True)


def make_practical_post(post, slot_label, campaign):
    page_url = tracked_site_url(post["path"], campaign, post["content"])
    lines = [
        f"<b>📶 WiFi Sonar · {slot_label}</b>",
        f"<b>{html_text(post['title'])}</b>",
        "",
        html_text(post["body"]),
        "",
        f"<b>Что сделать:</b> {html_text(post['action'])}",
        "",
        f"<a href=\"{html_text(page_url)}\">Пошаговая инструкция и WiFi Sonar ↗</a>",
        f"<i>Проверьте доступность интернета Фридом по адресу:</i> <a href=\"{html_text(FREEDOM_URL)}\">Фридом ↗</a>",
    ]
    return "\n".join(lines)


def maybe_publish_content():
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
                if now - datetime.fromisoformat(last_attempt) < timedelta(minutes=10):
                    continue
            except ValueError:
                pass
        state["attempts"][slot_key] = now.isoformat()
        save_state(state)

        items = []
        campaign = f"wifi_sonar_{slot_name}_{now.strftime('%Y%m%d')}"
        site_path = GUIDE_ROOT
        try:
            label = f"{slot_name}, {slot_start.strftime('%H:%M')} · UTC+3"
            if slot_name == "утро":
                ranked = collect_ranked()
                items = choose_items(ranked, state, now)
                if len(items) < 4:
                    log.warning("Only %d eligible articles for morning digest; will retry", len(items))
                    return
                items = translate_items_to_russian(items)
                guide_urls = [tracked_site_url(guide_for_item(item), campaign, f"story_{index}") for index, item in enumerate(items, 1)]
                message = make_digest(
                    items,
                    label,
                    html_text(tracked_site_url(GUIDE_ROOT, campaign, "guide_hub")),
                    html_text(FREEDOM_URL),
                    [html_text(url) for url in guide_urls],
                )
            else:
                posts = DAY_TIPS if slot_name == "день" else EVENING_EXPLAINERS
                offset = 0 if slot_name == "день" else 2
                post = posts[(now.date().toordinal() + offset) % len(posts)]
                site_path = post["path"]
                message = make_practical_post(post, label, campaign)
            api("sendMessage", {
                "chat_id": CHANNEL_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "reply_markup": button(campaign=campaign, site_path=site_path),
            })
            state["sent"][slot_key] = now.isoformat()
            state.setdefault("recent", []).extend({"url": canonical_url(item["url"]), "at": now.isoformat()} for item in items)
            save_state(state)
            log.info("Published %s content%s", slot_name, f" with {len(items)} articles" if items else "")
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, KeyError):
            log.exception("Could not publish %s digest", slot_name)


def handle(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text.startswith("/") else ""
    if command == "/start":
        send(chat_id, "Привет! В @wifisonar выходят утренний дайджест, дневные советы по Wi‑Fi и вечерние разборы.\n"
                      "Нажми «Подписаться на канал», чтобы получать новые материалы.\n"
                      "Команда /app — открыть инструкции и страницу WiFi Sonar.\n"
                      "Команда /recommend — пример «Фридом» по теме или региону.\n\n"
                      "Публикации выходят в 08:00, 13:00 и 18:00 по UTC+3.\n\n"
                      "Команды: /go — инструкции на сайте, /buzz — повод заглянуть, /help — помощь",
             campaign="wifi_sonar_bot_start")
    elif command == "/go":
        send(chat_id, "Твоя точка приземления готова 🛬", campaign="wifi_sonar_bot_go")
    elif command == "/app":
        send(chat_id, "WiFi Sonar помогает измерять сигнал, сравнивать каналы, проверять DNS, шлюз и задержку, а также отмечать покрытие на плане помещения.\n\n"
                      "На сайте — практические инструкции, описание разрешений и APK для Android 8.0 и новее. Доступность показаний зависит от устройства и версии Android.",
             campaign="wifi_sonar_bot_app_command")
    elif command == "/buzz":
        send(chat_id, random.choice(HOOKS), campaign="wifi_sonar_bot_buzz")
    elif command == "/help":
        send(chat_id, "В канале три формата: утренний дайджест, дневной практический совет и вечерний разбор.\n"
                      "/recommend [тема или регион] — пример на сайте Фридом\n"
                      "/app — инструкции и страница WiFi Sonar\n"
                      "/subscribe — ссылка на канал с выпусками\n"
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
            {"command": "app", "description": "Инструкции и страница WiFi Sonar"},
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
                "allowed_updates": '["message"]',
            })
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    try:
                        handle(update["message"])
                    except (urllib.error.URLError, KeyError, ValueError, RuntimeError):
                        log.exception("Could not handle update %s", update["update_id"])
            maybe_publish_content()
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            log.exception("Polling failed; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
