"""Collect popular networking stories and format a source-attributed digest."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import json
import logging
import math
import re
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
    "network", "networking", "internet", "wifi", "wi-fi", "wireless",
    "router", "routing", "bgp", "dns", "tcp", "udp", "ipv4", "ipv6",
    "ethernet", "quic", "firewall", "vpn", "wireguard", "proxy",
    "subnet", "packet", "datacenter", "data center", "5g", "6g",
    "mesh network", "load balancer", "ddos", "cloudflare", "fiber optic",
    "switching", "network security", "tls", "internet exchange", "peering",
)
_WORD_RE = re.compile(r"[a-z0-9]+(?:[- ][a-z0-9]+)?", re.I)


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


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


def topic_terms(text):
    haystack = " " + re.sub(r"[^a-z0-9+.-]+", " ", (text or "").casefold()) + " "
    return [term for term in KEYWORDS if term in haystack]


def summary_from(value, fallback):
    text = clean_text(value)
    if not text:
        return fallback
    sentences = re.split(r"(?<=[.!?])\s+", text)
    excerpt = " ".join(sentences[:2]).strip()
    if len(excerpt) > 300:
        excerpt = excerpt[:297].rsplit(" ", 1)[0] + "…"
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
        if age_hours > 72:
            return None
        points = int(item.get("score", 0))
        comments = int(item.get("descendants", 0))
        popularity = (points + comments * 1.5 + 1) / math.sqrt(age_hours + 4)
        fallback = f"{points} баллов и {comments} комментариев на Hacker News; тема — {', '.join(terms[:3])}."
        article_url = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
        if not is_http_url(article_url):
            article_url = f"https://news.ycombinator.com/item?id={story_id}"
        return {
            "title": title,
            "url": article_url,
            "summary": summary_from(item.get("text", ""), fallback),
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
    for listing in ("topstories", "beststories"):
        try:
            ids.extend(request_json(f"{HN_API}/{listing}.json")[:50])
        except (urllib.error.URLError, TimeoutError, ValueError):
            log.exception("Could not fetch Hacker News %s", listing)
    unique_ids = list(dict.fromkeys(ids))[:70]
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
            except (TypeError, ValueError, OverflowError):
                try:
                    published = datetime.fromisoformat(fields[date_field].replace("Z", "+00:00"))
                    break
                except (TypeError, ValueError, OverflowError):
                    continue
        age_hours = 24.0
        if published:
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 3600)
            if age_hours > 72:
                continue
        fallback = f"Рекомендовано сообществом {source}; тема — {', '.join(terms[:3])}."
        items.append({
            "title": title,
            "url": link,
            "summary": summary_from(summary, fallback),
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
    return sorted(deduped.values(), key=lambda item: item["popularity"], reverse=True)


def translate_items_to_russian(items):
    texts = []
    for item in items:
        texts.extend((item["title"], item["summary"]))
    payload = urllib.parse.urlencode(
        [("q", text) for text in texts] + [
            ("source", "auto"), ("target", "ru"), ("format", "text")
        ]
    ).encode()
    request = urllib.request.Request(
        TRANSLATE_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
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
        return "Даже хороший Wi‑Fi зависит от линии до дома — проверьте, доступен ли интернет Фридом по вашему адресу."
    if terms & {"fiber optic", "ethernet", "bgp", "routing", "internet exchange", "peering"}:
        return "Практический контекст — доступность и стабильность линии до дома; проверьте покрытие Фридом по адресу."
    if terms & {"vpn", "wireguard", "firewall", "ddos", "network security", "tls"}:
        return "Безопасность начинается с надёжного подключения; проверьте доступность интернета Фридом по адресу."
    return "Технология важна и в повседневном подключении — проверьте, доступен ли интернет Фридом по вашему адресу."


def make_digest(items, slot_label, site_url, freedom_url):
    if not items:
        return ""
    lines = [f"<b>📡 WiFiSonar · {slot_label}</b>", "<i>3 популярных материала о сетях и интернете</i>", ""]
    for index, item in enumerate(items[:3], 1):
        title = clean_text(item["title"])
        summary = clean_text(item["summary"])
        title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        summary = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        article_url = item["url"].replace("&", "&amp;").replace('"', "&quot;")
        lines.append(f"<b>{index}. <a href=\"{article_url}\">{title}</a></b>")
        lines.append(f"{summary}")
        if item.get("points") is not None:
            lines.append(f"<i>Hacker News · {item['points']} баллов · {item['comments']} комментариев</i>")
        else:
            lines.append(f"<i>{item['source']}</i>")
        bridge = freedom_bridge(item).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"<i>{bridge}</i> <a href=\"{freedom_url}\">Фридом ↗</a>")
        lines.append("")
    if site_url:
        lines.append(f"<a href=\"{site_url}\">Основной сайт ↗</a>")
    return "\n".join(lines)
