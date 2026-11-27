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

from digest import canonical_url, collect_ranked, make_digest, make_evening_news_digest, translate_items_to_russian

TOKEN = os.environ["BOT_TOKEN"]
APK_REPO = os.getenv("APK_REPO", "mgnkick/sonar")
APK_WORKFLOW_PATH = os.getenv("APK_WORKFLOW_PATH", ".github/workflows/rustore-release.yml")
APK_CACHE_TTL = int(os.getenv("APK_CACHE_TTL", "900"))
APK_CACHE = {"checked_at": 0, "url": None, "version": None, "tag": None}
TEST_CHAT_ID = os.getenv("TEST_CHAT_ID", "").strip()
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
DONATE_ETH_ADDRESS = os.getenv("DONATE_ETH_ADDRESS") or "0x3621BA1773f64aBd909f89c21Df5149d9Cf6F3d1"
if not re.fullmatch(r"0x[a-fA-F0-9]{40}", DONATE_ETH_ADDRESS):
    log.warning("ETH donation address is missing or invalid; hiding ETH option")


def button(include_channel=False, campaign="bot_menu", site_path=GUIDE_ROOT):
    apk = latest_apk()
    download_row = ([{"text": "⬇️ Скачать актуальный APK", "url": apk["url"]}]
                    if apk and apk.get("url") else
                    [{"text": "Скачать APK на сайте ↗", "url": tracked_site_url("/", campaign, "app_page")}])
    keyboard = [
        download_row,
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
        return ("Спасибо, что хотите поддержать развитие WiFi Sonar! ❤️\n\n"
                "Реквизиты пока настраиваются. Загляните немного позже.")
    return ("Спасибо за интерес к поддержке WiFi Sonar! ❤️\n\n"
            "Можно поддержать проект " + " или ".join(methods) + ".\n"
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
        campaign = f"wifi_sonar_{slot_name}_{now.strftime('%Y%m%d')}"
        site_path = GUIDE_ROOT
        items = []
        try:
            label = f"{slot_name}, {slot_start.strftime('%H:%M')} · UTC+3"
            if slot_name == "утро":
                message, items = build_morning_digest(now, campaign)
            elif slot_name == "день":
                post = DAY_TIPS[(now.date().toordinal()) % len(DAY_TIPS)]
                site_path = post["path"]
                message = make_practical_post(post, label, campaign)
            else:
                # Keep the 18:00 practical explainer and publish a separate news roundup.
                post = EVENING_EXPLAINERS[(now.date().toordinal() + 2) % len(EVENING_EXPLAINERS)]
                site_path = post["path"]
                practical = make_practical_post(post, label, campaign)
                ranked = collect_ranked()
                items = choose_items(ranked, state, now)
                if len(items) < 4:
                    raise RuntimeError(f"Only {len(items)} eligible articles for evening digest")
                translated = translate_items_to_russian(items)
                apk = latest_apk()
                news = make_evening_news_digest(
                    translated, f"вечер · {slot_start.strftime('%H:%M')} · UTC+3",
                    html_text(FREEDOM_URL), html_text(apk.get("url", "")),
                )
                message = practical + "\\n\\n" + news
            api("sendMessage", {
                "chat_id": CHANNEL_ID, "text": message, "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "reply_markup": button(campaign=campaign, site_path=site_path),
            })
            state["sent"][slot_key] = now.isoformat()
            state.setdefault("recent", []).extend(
                {"url": canonical_url(item["url"]), "at": now.isoformat()} for item in items
            )
            save_state(state)
            log.info("Published %s content%s", slot_name, f" with {len(items)} articles" if items else "")
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, KeyError):
            log.exception("Could not publish %s digest", slot_name)


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
            "text": (f"❤️ Поддержать WiFi Sonar · {coin}\n\n"
                     f"Сеть: {network_names[coin]}\n"
                     f"Адрес кошелька:\n<code>{escape(address)}</code>\n\n"
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
        text = "❤️ Поддержать WiFi Sonar · Сбербанк\n\n"
        text += f"Карта: <code>{escape(card)}</code>\n"
        if recipient:
            text += f"Получатель: {escape(recipient)}\n"
        text += "\nПеревод в рублях. Спасибо за поддержку!"
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



def github_json(url, timeout=15):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "t-bot-sonar/1.0",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def latest_apk(force=False):
    now = time.time()
    if not force and now - APK_CACHE["checked_at"] < APK_CACHE_TTL:
        return APK_CACHE
    try:
        runs = github_json(
            f"https://api.github.com/repos/{APK_REPO}/actions/runs?per_page=100"
        ).get("workflow_runs", [])
        good_runs = [
            run for run in runs
            if run.get("path") == APK_WORKFLOW_PATH
            and run.get("head_branch") == "main"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
        ]
        for run in good_runs:
            releases = github_json(
                f"https://api.github.com/repos/{APK_REPO}/releases?per_page=100"
            )
            if isinstance(releases, dict):
                releases = releases.get("items", [])
            release = next((item for item in releases
                            if not item.get("draft")
                            and item.get("target_commitish") == run.get("head_sha")), None)
            if not release:
                continue
            asset = next((item for item in release.get("assets", [])
                          if item.get("name") == "WiFi-Sonar-release.apk"), None)
            if asset and asset.get("browser_download_url"):
                tag = release.get("tag_name", "")
                match = re.search(r"wifi-sonar-v(.+?)-build-", tag)
                APK_CACHE.update({
                    "checked_at": now,
                    "url": asset["browser_download_url"],
                    "version": match.group(1) if match else release.get("name", "последняя"),
                    "tag": tag,
                })
                return APK_CACHE
        raise RuntimeError("No signed APK release for successful main workflow run")
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, RuntimeError) as exc:
        log.warning("Could not resolve latest signed APK: %s", exc, exc_info=True)
        APK_CACHE["checked_at"] = now
        return APK_CACHE


def apk_link_text():
    apk = latest_apk(force=True)
    if not apk.get("url"):
        return "Не удалось получить ссылку на последнюю подписанную сборку. Попробуйте позже."
    return f"⬇️ WiFi Sonar {apk['version']} — скачать APK:\\n{apk['url']}"


def build_morning_digest(now, campaign):
    ranked = collect_ranked()
    state = load_state()
    items = choose_items(ranked, state, now)
    if len(items) < 4:
        raise RuntimeError(f"Недостаточно материалов для дайджеста: {len(items)}")
    translated = translate_items_to_russian(items)
    guide_urls = [tracked_site_url(guide_for_item(item), campaign, f"story_{i}")
                  for i, item in enumerate(translated, 1)]
    body = make_digest(
        translated, f"утро, {now.strftime('%H:%M')} · UTC+3",
        html_text(tracked_site_url(GUIDE_ROOT, campaign, "guide_hub")),
        html_text(FREEDOM_URL), [html_text(url) for url in guide_urls],
    )
    return body, items


def send_test_cycle(chat_id):
    now = datetime.now(UTC_PLUS_3)
    # Test cycle may only run in a private chat; never emit fixtures to the public channel.
    if str(chat_id) == str(CHANNEL_ID) or str(chat_id).startswith("-100"):
        raise RuntimeError("Test cycle is restricted to a private administrator chat")
    campaign = f"wifi_sonar_test_{now.strftime('%Y%m%d_%H%M%S')}"
    results = []
    try:
        apk = latest_apk(force=True)
        results.append("APK: " + (apk.get("version") if apk.get("url") else "не найден"))
        morning, _ = build_morning_digest(now, campaign)
        ranked = collect_ranked()
        evening_items = choose_items(ranked, {"recent": []}, now)[:5]
        if len(evening_items) < 4:
            raise RuntimeError("Недостаточно материалов для вечернего дайджеста")
        evening_items = translate_items_to_russian(evening_items)
        day_post = DAY_TIPS[now.date().toordinal() % len(DAY_TIPS)]
        evening_post = EVENING_EXPLAINERS[(now.date().toordinal() + 2) % len(EVENING_EXPLAINERS)]
        day_body = make_practical_post(day_post, f"ТЕСТ · день · {now:%H:%M} UTC+3", campaign)
        explain_body = make_practical_post(evening_post, f"ТЕСТ · вечерний разбор · {now:%H:%M} UTC+3", campaign)
        news_body = make_evening_news_digest(
            evening_items, f"ТЕСТ · вечерний дайджест · {now:%H:%M} UTC+3",
            html_text(FREEDOM_URL), html_text(apk.get("url", "")),
        )
        for label, body in (
            ("Утренний дайджест", morning),
            ("Дневной совет", day_body),
            ("Вечерний разбор", explain_body),
            ("Вечерний новостной дайджест", news_body),
        ):
            api("sendMessage", {
                "chat_id": chat_id, "text": body, "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "reply_markup": button(campaign=campaign),
            })
            results.append(label + ": OK")
        api("sendMessage", {"chat_id": chat_id, "text": "Тестовый цикл завершён.\\n" + "\\n".join(results)})
    except Exception as exc:
        log.exception("Test daily cycle failed")
        api("sendMessage", {"chat_id": chat_id, "text": "Тестовый цикл остановлен: " + str(exc)[:500]
                            + "\\n" + "\\n".join(results)})


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
        apk = latest_apk(force=True)
        if apk.get("url"):
            extra = f"\\n\\nАктуальная подписанная версия WiFi Sonar {apk['version']}:\\n{apk['url']}"
        else:
            extra = "\\n\\nСсылка на последнюю подписанную сборку временно недоступна."
        send(chat_id, "WiFi Sonar помогает измерять сигнал, сравнивать каналы, проверять DNS, шлюз и задержку, а также отмечать покрытие на плане помещения.\\n\\n"
                      "На сайте — инструкции и описание разрешений. Приложение рассчитано на Android 8.0 и новее." + extra,
             campaign="wifi_sonar_bot_app_command")
    elif command == "/donate":
        send_donation(chat_id, donation_text(), donation_keyboard())
    elif command == "/apk":
        send(chat_id, apk_link_text(), campaign="wifi_sonar_bot_apk")
    elif command == "/testday":
        if not TEST_CHAT_ID or str(chat_id) != TEST_CHAT_ID:
            send(chat_id, "Тестовый прогон доступен только администратору.")
        else:
            send_test_cycle(chat_id)
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
            {"command": "app", "description": "Сайт и актуальный APK"},\n            {"command": "apk", "description": "Скачать актуальный APK"},\n            {"command": "testday", "description": "Тестовый прогон дневного цикла"},
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
