"""Collect popular networking stories and format a source-attributed digest."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import json
import ipaddress
import logging
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("t-bot-sonar.digest")
HN_API = "https://hacker-news.firebaseio.com/v0"
TRANSLATE_URL = "http://127.0.0.1:5000/translate"
LOBSTERS_FEEDS = (
    ("Lobsters · главная", "https://lobste.rs/rss"),
    ("Lobsters · networking", "https://lobste.rs/t/networking.rss"),
    ("Lobsters · security", "https://lobste.rs/t/security.rss"),
)
KEYWORDS = (
    "networking", "computer network", "computer networking", "network", "networks",
    "network protocol", "network infrastructure", "network performance", "network equipment",
    "network hardware", "network stack", "network traffic", "network outage", "access network",
    "network security", "network operator", "internet", "internet access",
    "internet service provider", "internet outage", "wifi", "wi-fi", "wireless",
    "wireless lan", "wlan", "router", "routers", "routing", "bgp", "dns",
    "wifi channel", "wi-fi channel", "wireless channel", "channel congestion",
    "tcp", "udp", "ipv4", "ipv6", "ethernet", "quic", "firewall", "vpn",
    "wireguard", "proxy", "subnet", "packet", "datacenter", "data center",
    "5g", "6g", "mesh network", "load balancer", "ddos", "cloudflare",
    "fiber optic", "fibre optic", "optical fiber", "optical fibre",
    "switching", "internet exchange", "peering", "broadband", "telecom",
    "telecommunications", "isp", "access point", "access points", "cellular",
    "submarine cable", "undersea cable", "sd-wan", "bandwidth", "latency",
    "network management", "satellite internet", "internet infrastructure", "internet protocol",
    "internet connectivity", "fiber internet", "wireless networking", "communication protocol",
)
_WORD_RE = re.compile(r"[a-z0-9]+(?:[- ][a-z0-9]+)?", re.I)


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def visible_text_length(html_text):
    parser = TextOnly()
    parser.feed(html_text)
    visible = "".join(parser.parts)
    return len(visible.encode("utf-16-le")) // 2


class MetaDescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.descriptions = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        key = (values.get("property") or values.get("name") or "").lower()
        content = clean_text(values.get("content", ""))
        if content and key in ("og:description", "twitter:description", "description"):
            self.descriptions.setdefault(key, content)


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if not is_public_http_url(new_url):
            raise urllib.error.URLError("Blocked non-public article redirect")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def clean_text(value):
    parser = TextOnly()
    try:
        parser.feed(unescape(value or ""))
    except (ValueError, AssertionError):
        pass
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def request_bytes(url, timeout=18):
    request = urllib.request.Request(url, headers={
        "User-Agent": "t-bot-sonar/1.0 (+https://github.com/mgnkick/t-bot-sonar)",
        "Accept": "application/json, application/rss+xml, application/xml, text/xml",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(2_000_000)


def request_json(url):
    return json.loads(request_bytes(url))


def is_http_url(value):
    try:
        parts = urllib.parse.urlsplit(value)
        return parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username and not parts.password
    except ValueError:
        return False


def is_public_http_url(value):
    if not is_http_url(value):
        return False
    parts = urllib.parse.urlsplit(value)
    host = parts.hostname
    try:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            port = parts.port or (443 if parts.scheme == "https" else 80)
            addresses = [
                ipaddress.ip_address(info[4][0].split("%", 1)[0])
                for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            ]
        return bool(addresses) and all(address.is_global for address in addresses)
    except (OSError, ValueError):
        return False


def fetch_meta_description(url):
    if not is_public_http_url(url):
        return ""
    request = urllib.request.Request(url, headers={
        "User-Agent": "t-bot-sonar/1.0 (+https://github.com/mgnkick/t-bot-sonar)",
        "Accept": "text/html, application/xhtml+xml",
    })
    opener = urllib.request.build_opener(PublicRedirectHandler())
    try:
        with opener.open(request, timeout=6) as response:
            content_type = response.headers.get_content_type()
            if content_type not in ("text/html", "application/xhtml+xml"):
                return ""
            raw = response.read(512_000)
            encoding = response.headers.get_content_charset() or "utf-8"
        parser = MetaDescriptionParser()
        parser.feed(raw.decode(encoding, errors="replace"))
        return (parser.descriptions.get("og:description")
                or parser.descriptions.get("twitter:description")
                or parser.descriptions.get("description", ""))
    except (urllib.error.URLError, TimeoutError, UnicodeError, ValueError, OSError):
        log.debug("Could not fetch article description for %s", url, exc_info=True)
        return ""


def topic_terms(text):
    haystack = re.sub(r"[^a-z0-9]+", " ", (text or "").casefold())
    found = []
    for term in KEYWORDS:
        normalized = re.sub(r"[^a-z0-9]+", " ", term.casefold()).strip()
        pattern = r"(?<![a-z0-9])" + re.escape(normalized).replace(" ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, haystack):
            found.append(term)
    # Do not let an unqualified social/neural network mention pass as networking news.
    generic = {"network", "networks"}
    if set(found) and set(found) <= generic:
        return []
    return found


def summary_from(value, fallback):
    text = clean_text(value)
    if not text:
        return fallback
    sentences = re.split(r"(?<=[.!?])\s+", text)
    excerpt = " ".join(sentences[:4]).strip()
    if len(excerpt) > 600:
        excerpt = excerpt[:597].rsplit(" ", 1)[0] + "…"
    return excerpt or fallback


def fetch_hn_item(story_id):
    try:
        item = request_json(f"{HN_API}/item/{story_id}.json")
        if not item or item.get("type") != "story" or item.get("dead") or item.get("deleted"):
            return None
        title = clean_text(item.get("title", ""))
        if not title:
            return None
        terms = topic_terms(title + " " + clean_text(item.get("text", "")))
        if not terms:
            return None
        published = datetime.fromtimestamp(item.get("time", 0), tz=timezone.utc)
        age_hours = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
        if age_hours > 168:
            return None
        points = int(item.get("score", 0))
        comments = int(item.get("descendants", 0))
        popularity = (points + comments * 1.5 + 1) / math.sqrt(age_hours + 4)
        fallback = f"{points} баллов и {comments} комментариев на Hacker News; тема — {', '.join(terms[:3])}."
        story_text = clean_text(item.get("text", ""))
        article_url = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
        if not is_http_url(article_url):
            article_url = f"https://news.ycombinator.com/item?id={story_id}"
        return {
            "title": title,
            "url": article_url,
            "summary": summary_from(story_text, fallback),
            "has_content_summary": bool(story_text),
            "needs_meta_summary": not bool(story_text),
            "source": "Hacker News",
            "popularity": popularity,
            "age": age_hours,
            "terms": terms,
            "comments": comments,
            "points": points,
            "key": f"hn:{story_id}",
        }
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, KeyError):
        log.debug("Could not fetch Hacker News item %s", story_id, exc_info=True)
        return None


def collect_hn():
    ids = []
    for listing in ("topstories", "beststories", "showstories", "askstories", "newstories"):
        try:
            ids.extend(request_json(f"{HN_API}/{listing}.json")[:60])
        except (urllib.error.URLError, TimeoutError, ValueError):
            log.exception("Could not fetch Hacker News %s", listing)
    unique_ids = list(dict.fromkeys(ids))[:240]
    stories = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_hn_item, story_id) for story_id in unique_ids]
        for future in as_completed(futures):
            item = future.result()
            if item:
                stories.append(item)
    return stories


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_bytes, source, url):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        log.warning("Ignoring malformed feed: %s", url)
        return items

    for entry in root.iter():
        if local_name(entry.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in entry.iter():
            name = local_name(child.tag)
            if name in ("title", "description", "summary", "published", "updated", "pubdate", "link", "content"):
                fields.setdefault(name, child.text or "")
                if name == "link" and child.attrib.get("href"):
                    fields["link"] = child.attrib["href"]
        title = clean_text(fields.get("title", ""))
        link = (fields.get("link") or "").strip()
        if not title or not is_http_url(link):
            continue
        summary = clean_text(fields.get("summary") or fields.get("description") or fields.get("content") or "")
        terms = topic_terms(title + " " + summary)
        if not terms:
            continue
        published = None
        for date_field in ("published", "updated", "pubdate"):
            try:
                published = parsedate_to_datetime(fields[date_field])
                break
            except (KeyError, TypeError, ValueError, OverflowError):
                try:
                    published = datetime.fromisoformat(fields[date_field].replace("Z", "+00:00"))
                    break
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
        age_hours = 24.0
        if published:
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 3600)
            if age_hours > 168:
                continue
        fallback = f"Рекомендовано сообществом {source}; тема — {', '.join(terms[:3])}."
        items.append({
            "title": title,
            "url": link,
            "summary": summary_from(summary, fallback),
            "has_content_summary": bool(summary),
            "needs_meta_summary": not bool(summary),
            "source": source,
            "age": age_hours,
            "terms": terms,
        })
    return items


def collect_lobsters():
    stories = []
    for source, url in LOBSTERS_FEEDS:
        try:
            entries = parse_feed(request_bytes(url), source, url)
        except (urllib.error.URLError, TimeoutError, ValueError):
            log.exception("Could not fetch Lobsters feed %s", url)
            continue
        for rank, item in enumerate(entries[:40]):
            item["popularity"] = (25.0 / (rank + 1)) / math.sqrt(item["age"] + 4)
            item["key"] = "url:" + canonical_url(item["url"])
            stories.append(item)
    return stories


def canonical_url(value):
    parts = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def collect_ranked():
    all_items = collect_hn() + collect_lobsters()
    deduped = {}
    for item in all_items:
        key = canonical_url(item["url"])
        old = deduped.get(key)
        if old is None or item["popularity"] > old["popularity"]:
            deduped[key] = item
    ranked = sorted(deduped.values(), key=lambda item: item["popularity"], reverse=True)
    needs_summary = [item for item in ranked if item.get("needs_meta_summary")][:10]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_meta_description, item["url"]): item for item in needs_summary}
        for future in as_completed(futures):
            item = futures[future]
            description = future.result()
            if description:
                item["summary"] = summary_from(description, item["summary"])
                item["has_content_summary"] = True
                item["needs_meta_summary"] = False
    # Prefer stories with an actual publisher/community description when ranking is close.
    return sorted(ranked, key=lambda item: (not item.get("has_content_summary", False), -item["popularity"]))


def translate_items_to_russian(items):
    texts = []
    for item in items:
        texts.extend((item["title"], item["summary"]))
    payload = json.dumps({
        "q": texts,
        "source": "auto",
        "target": "ru",
        "format": "text",
    }).encode()
    request = urllib.request.Request(
        TRANSLATE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read())
        translated = result.get("translatedText")
        if isinstance(translated, str):
            translated = [translated]
        if not isinstance(translated, list) or len(translated) != len(texts):
            raise ValueError("Unexpected translation response")
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError) as exc:
        raise RuntimeError("Russian translation service is unavailable") from exc
    result_items = []
    for index, item in enumerate(items):
        translated_item = dict(item)
        translated_item["title"] = translated[index * 2].strip() or item["title"]
        translated_item["summary"] = translated[index * 2 + 1].strip() or item["summary"]
        result_items.append(translated_item)
    return result_items


def freedom_bridge(item):
    terms = set(item.get("terms", []))
    if terms & {"wifi", "wi-fi", "wireless", "mesh network"}:
        return "Wi‑Fi зависит и от линии до дома — проверьте покрытие Фридом по адресу."
    if terms & {"fiber optic", "ethernet", "bgp", "routing", "internet exchange", "peering"}:
        return "Стабильность зависит от линии до дома — проверьте покрытие Фридом."
    if terms & {"vpn", "wireguard", "firewall", "ddos", "network security", "tls"}:
        return "Надёжность начинается с линии доступа — проверьте покрытие Фридом."
    return "Проверьте, доступен ли интернет Фридом по вашему адресу."


def app_bridge(item):
    terms = set(item.get("terms", []))
    if terms & {"wifi", "wi-fi", "wireless", "mesh network"}:
        return "WiFi Sonar покажет сигнал и загруженность Wi‑Fi-каналов."
    if terms & {"dns", "tcp", "udp", "routing", "bgp", "vpn", "wireguard", "firewall", "ddos"}:
        return "WiFi Sonar проверит DNS, шлюз, задержку и параметры подключения."
    return "WiFi Sonar поможет проверить Wi‑Fi и качество подключения."


def why_in_digest(item):
    if item.get("points") is not None:
        return (f"Hacker News: {item['points']} баллов, "
                f"{item.get('comments', 0)} комментариев.")
    age = max(1, round(item.get("age", 24)))
    return (f"Свежая тематическая публикация в {item.get('source', 'источнике')}, "
            f"около {age} ч назад.")


def reader_context(item):
    terms = set(item.get("terms", []))
    if terms & {"wifi", "wi-fi", "wireless", "mesh network", "router"}:
        return "Wi‑Fi и роутеры влияют на стабильность интернета дома и в офисе."
    if terms & {"dns"}:
        return "DNS связывает имена сайтов с IP-адресами; его сбой мешает открывать сервисы."
    if terms & {"bgp", "routing", "internet exchange", "peering"}:
        return "Маршрутизация выбирает путь данных и влияет на доступность и задержки."
    if terms & {"vpn", "wireguard", "firewall", "ddos", "network security", "tls"}:
        return "Эти технологии защищают трафик и влияют на безопасность доступа к сервисам."
    if terms & {"fiber optic", "ethernet", "datacenter", "data center"}:
        return "Оптика, Ethernet и дата-центры задают пропускную способность и надёжность сервисов."
    if terms & {"tcp", "udp", "quic", "packet"}:
        return "Протоколы доставки данных влияют на скорость и отзывчивость приложений."
    return "Это часть инфраструктуры, от которой зависят скорость и доступность интернет-сервисов."


def make_digest(items, slot_label, site_url, freedom_url, app_urls):
    if not items:
        return ""

    def clip(value, limit):
        value = clean_text(value)
        if len(value) <= limit:
            return value
        return value[:limit - 1].rsplit(" ", 1)[0] + "…"

    lines = [f"<b>📡 WiFiSonar · {slot_label}</b>", f"<i>{len(items)} популярных материалов о сетях и интернете</i>", ""]
    summary_slots = []
    for index, item in enumerate(items[:5], 1):
        title = clip(item["title"], 120)
        summary = clean_text(item["summary"])
        title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        summary = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        article_url = item["url"].replace("&", "&amp;").replace('"', "&quot;")
        lines.append(f"<b><a href=\"{article_url}\">{title}</a></b>")
        if item.get("has_content_summary"):
            label = "Выжимка"
            summary_text = clean_text(item["summary"])
        else:
            label = "О чём материал"
            summary_text = "Источник не предоставил описание. " + clean_text(item["summary"])
        summary_slots.append((len(lines), label, summary_text))
        lines.append(f"<b>{label}:</b> {summary}")
        why = clip(why_in_digest(item), 100).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        context = clip(reader_context(item), 110).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"<b>Почему сейчас:</b> {why}")
        lines.append(f"<b>Контекст:</b> {context}")
        bridge = clip(freedom_bridge(item), 85).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"<i>{bridge}</i> <a href=\"{freedom_url}\">Фридом. Интернет друзей. ↗</a>")
        app_note = clip(app_bridge(item), 80).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        app_url = app_urls[index - 1]
        lines.append(f"<i>{app_note}</i> <a href=\"{app_url}\">Сайт приложения ↗</a>")
        lines.append("")
    if site_url:
        lines.append(f"<a href=\"{site_url}\">Сайт приложения ↗</a>")
    message = "\n".join(lines)
    for limit in (180, 150, 120, 90, 60):
        if visible_text_length(message) <= 3900:
            break
        for line_index, label, summary_text in summary_slots:
            excerpt = summary_text[:limit]
            if len(summary_text) > limit:
                excerpt = excerpt.rsplit(" ", 1)[0] + "…"
            escaped_excerpt = excerpt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines[line_index] = f"<b>{label}:</b> {escaped_excerpt}"
        message = "\n".join(lines)
    if visible_text_length(message) > 3900:
        raise ValueError("Digest exceeds Telegram message limit")
    return message


def make_evening_news_digest(items, slot_label, freedom_url, apk_url):
    """Compact evening news roundup with source attribution and app download link."""
    if not items:
        return ""
    lines = [
        f"<b>🌙 WiFi Sonar · {slot_label}</b>",
        f"<i>{len(items[:5])} главных материалов дня о сетях, связи и интернете</i>",
        "",
    ]
    for index, item in enumerate(items[:5], 1):
        title = clean_text(item["title"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        summary = clean_text(item.get("summary", "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        url = item["url"].replace("&", "&amp;").replace('"', "&quot;")
        lines.append(f"{index}. <b><a href=\"{url}\">{title}</a></b>")
        lines.append(summary)
        lines.append(f"<i>{why_in_digest(item)}</i>")
        lines.append("")
    lines.append(f"<a href=\"{freedom_url}\">Фридом. Интернет друзей. ↗</a>")
    if apk_url:
        lines.append(f"<a href=\"{apk_url}\">⬇️ Скачать актуальный WiFi Sonar APK</a>")
    message = "\n".join(lines)
    if visible_text_length(message) > 3900:
        raise ValueError("Evening digest exceeds Telegram message limit")
    return message
