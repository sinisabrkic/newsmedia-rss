#!/usr/bin/env python3
"""Generate the official RSS 2.0 feed for newsmedia.report.

The script discovers Webador article URLs, extracts structured metadata, caches
older entries, and writes a standards-compliant teaser feed to rss.xml.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import mimetypes
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
ARTICLE_ID_RE = re.compile(r"(?:^|/)(\d{5,})_[^/?#]+(?:$|[?#])", re.IGNORECASE)
DATE_TEXT_RE = re.compile(
    r"(?:Veröffentlicht\s+am|Published\s+(?:on)?)\s+(.{4,90}?)(?=(?:\n|\r|Section:|Rubrik:|Format:|Author:|Autor:|$))",
    re.IGNORECASE | re.DOTALL,
)
AUTHOR_RE = re.compile(r"(?:Author|Autor)\s*:\s*([^\n\r]{2,100})", re.IGNORECASE)
CATEGORY_RE = re.compile(r"(?:Section|Rubrik)\s*:\s*([^\n\r]{2,140})", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_MEDIA = "http://search.yahoo.com/mrss/"
NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
NS_DC = "http://purl.org/dc/elements/1.1/"


@dataclass
class Article:
    url: str
    title: str
    published: str
    description: str
    author: str
    category: str = ""
    image: str = ""
    language: str = ""
    article_id: int = 0

    @property
    def published_dt(self) -> datetime:
        value = datetime.fromisoformat(self.published)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class FeedError(RuntimeError):
    pass


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return SPACE_RE.sub(" ", html.unescape(str(value))).strip()


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc.lower()
    if host == "newsmedia.report":
        host = "www.newsmedia.report"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, host, path, "", "", ""))


def article_id(url: str) -> int:
    match = ARTICLE_ID_RE.search(urlparse(url).path)
    return int(match.group(1)) if match else 0


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    required = ["site_url", "feed_url", "output_file", "state_file", "max_items"]
    missing = [key for key in required if key not in config]
    if missing:
        raise FeedError(f"Missing config keys: {', '.join(missing)}")
    return config


def make_session(config: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": config.get("user_agent", "newsmedia.report RSS Generator/1.0"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "Cache-Control": "no-cache",
        }
    )
    return session


def fetch(session: requests.Session, url: str, *, timeout: int = 30) -> requests.Response:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response


def is_same_site(url: str, site_url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    site_host = urlparse(site_url).netloc.lower().removeprefix("www.")
    return host == site_host


def extract_urls_from_sitemap(xml_bytes: bytes, base_url: str) -> tuple[list[str], list[str]]:
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return [], []

    page_urls: list[str] = []
    sitemap_urls: list[str] = []
    root_name = etree.QName(root).localname.lower()
    for element in root.xpath("//*[local-name()='loc']"):
        value = clean_text(element.text)
        if not value:
            continue
        absolute = urljoin(base_url, value)
        if root_name == "sitemapindex" or absolute.lower().endswith((".xml", ".xml.gz")):
            sitemap_urls.append(absolute)
        else:
            page_urls.append(absolute)
    return page_urls, sitemap_urls


def discover_from_sitemaps(
    session: requests.Session, site_url: str, max_sitemaps: int = 12
) -> list[str]:
    seeds = [
        urljoin(site_url, "sitemap.xml"),
        urljoin(site_url, "sitemap_index.xml"),
        urljoin(site_url, "sitemap-index.xml"),
    ]
    queue = list(seeds)
    visited: set[str] = set()
    found: set[str] = set()

    while queue and len(visited) < max_sitemaps:
        sitemap_url = queue.pop(0)
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        try:
            response = fetch(session, sitemap_url)
        except requests.RequestException as exc:
            logging.debug("Sitemap unavailable %s: %s", sitemap_url, exc)
            continue
        pages, nested = extract_urls_from_sitemap(response.content, sitemap_url)
        for page in pages:
            page = canonicalize_url(page)
            if is_same_site(page, site_url) and article_id(page):
                found.add(page)
        for child in nested:
            if is_same_site(child, site_url) and child not in visited:
                queue.append(child)

    return sorted(found, key=article_id, reverse=True)


def extract_article_links(html_text: str, base_url: str, site_url: str) -> set[str]:
    soup = BeautifulSoup(html_text, "lxml")
    links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        absolute = canonicalize_url(urljoin(base_url, anchor.get("href", "")))
        if is_same_site(absolute, site_url) and article_id(absolute):
            links.add(absolute)
    return links


def discover_from_pages(
    session: requests.Session, site_url: str, page_limit: int = 8
) -> list[str]:
    queue = [site_url]
    visited: set[str] = set()
    found: set[str] = set()

    while queue and len(visited) < page_limit:
        page_url = queue.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)
        try:
            response = fetch(session, page_url)
        except requests.RequestException as exc:
            logging.warning("Could not read discovery page %s: %s", page_url, exc)
            continue
        found.update(extract_article_links(response.text, response.url, site_url))

        soup = BeautifulSoup(response.text, "lxml")
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            text = clean_text(anchor.get_text(" ", strip=True)).lower()
            absolute = canonicalize_url(urljoin(response.url, href))
            if not is_same_site(absolute, site_url):
                continue
            query = urlparse(urljoin(response.url, href)).query.lower()
            looks_paginated = (
                text.isdigit()
                or "next" in text
                or "weiter" in text
                or "page=" in query
                or re.search(r"(?:^|&)(?:p|page)=\d+", query)
            )
            if looks_paginated and absolute not in visited and absolute not in queue:
                queue.append(urljoin(response.url, href))

    return sorted(found, key=article_id, reverse=True)


def discover_article_urls(session: requests.Session, config: dict[str, Any]) -> list[str]:
    site_url = config["site_url"]
    candidates = discover_from_sitemaps(session, site_url)
    page_candidates = discover_from_pages(session, site_url)
    merged = {canonicalize_url(url) for url in candidates + page_candidates if article_id(url)}
    ordered = sorted(merged, key=article_id, reverse=True)
    limit = int(config.get("candidate_limit", 90))
    logging.info(
        "Discovered %d article URLs (%d via sitemap, %d via pages); using newest %d",
        len(ordered),
        len(candidates),
        len(page_candidates),
        min(len(ordered), limit),
    )
    return ordered[:limit]


def iter_jsonld(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        nodes: list[Any]
        if isinstance(data, list):
            nodes = data
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            nodes = data["@graph"]
        else:
            nodes = [data]
        for node in nodes:
            if isinstance(node, dict):
                yield node


def meta_content(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        node = soup.find("meta", attrs={"property": key}) or soup.find(
            "meta", attrs={"name": key}
        )
        if node and node.get("content"):
            return clean_text(node["content"])
    return ""


def jsonld_value(nodes: Iterable[dict[str, Any]], key: str) -> Any:
    for node in nodes:
        value = node.get(key)
        if value:
            return value
    return None


def parse_author(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    if isinstance(value, list):
        names = [parse_author(item) for item in value]
        return ", ".join(name for name in names if name)
    return ""


MONTHS = {
    "januar": 1, "january": 1, "jan": 1,
    "februar": 2, "february": 2, "feb": 2,
    "märz": 3, "maerz": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6,
    "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "december": 12, "dez": 12, "dec": 12,
}


def parse_date(value: str, timezone_name: str) -> datetime | None:
    value = clean_text(value).replace(" Uhr", "")
    if not value:
        return None
    local_tz = ZoneInfo(timezone_name)

    iso_candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass

    normalized = re.sub(r"\b(?:um|at)\b", " ", value.lower())
    normalized = SPACE_RE.sub(" ", normalized).strip()
    patterns = [
        re.compile(r"^(\d{1,2})\.?\s+([a-zäöüß]+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?$", re.I),
        re.compile(r"^([a-zäöüß]+)\s+(\d{1,2}),?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?$", re.I),
        re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?:\s+(\d{1,2}):(\d{2}))?$"),
    ]

    match = patterns[0].match(normalized)
    if match:
        day, month_name, year, hour, minute = match.groups()
        month = MONTHS.get(month_name)
        if month:
            parsed = datetime(int(year), month, int(day), int(hour or 0), int(minute or 0), tzinfo=local_tz)
            return parsed.astimezone(timezone.utc)

    match = patterns[1].match(normalized)
    if match:
        month_name, day, year, hour, minute = match.groups()
        month = MONTHS.get(month_name)
        if month:
            parsed = datetime(int(year), month, int(day), int(hour or 0), int(minute or 0), tzinfo=local_tz)
            return parsed.astimezone(timezone.utc)

    match = patterns[2].match(normalized)
    if match:
        day, month, year, hour, minute = match.groups()
        parsed = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=local_tz)
        return parsed.astimezone(timezone.utc)

    return None


def plausible_description(text: str, title: str) -> bool:
    lowered = text.lower()
    blocked = (
        "zum hauptinhalt",
        "kommentar hinzufügen",
        "datenschutz",
        "all rights reserved",
        "weiterlesen",
        "cookie",
    )
    return (
        len(text) >= 70
        and len(text) <= 1200
        and text != title
        and not any(term in lowered for term in blocked)
    )


def first_article_paragraph(soup: BeautifulSoup, title: str) -> str:
    scopes = []
    for selector in ("article", "main", "[role='main']", ".jw-element-blog-post"):
        node = soup.select_one(selector)
        if node:
            scopes.append(node)
    scopes.append(soup)
    seen: set[str] = set()
    for scope in scopes:
        for node in scope.find_all("p"):
            text = clean_text(node.get_text(" ", strip=True))
            if text in seen:
                continue
            seen.add(text)
            if plausible_description(text, title):
                return text
    return ""


def extract_image(soup: BeautifulSoup, nodes: list[dict[str, Any]], page_url: str) -> str:
    image = meta_content(soup, "og:image", "twitter:image", "twitter:image:src")
    if not image:
        raw = jsonld_value(nodes, "image")
        if isinstance(raw, str):
            image = raw
        elif isinstance(raw, dict):
            image = raw.get("url") or raw.get("contentUrl") or ""
        elif isinstance(raw, list) and raw:
            first = raw[0]
            image = first if isinstance(first, str) else first.get("url", "") if isinstance(first, dict) else ""
    if not image:
        scope = soup.select_one("article, main, [role='main']") or soup
        for img in scope.find_all("img", src=True):
            candidate = img.get("src", "")
            width = str(img.get("width", ""))
            if candidate and not candidate.startswith("data:") and width != "1":
                image = candidate
                break
    return canonicalize_asset(urljoin(page_url, clean_text(image))) if image else ""


def canonicalize_asset(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, "", parsed.query, ""))


def parse_article(html_text: str, requested_url: str, config: dict[str, Any]) -> Article:
    soup = BeautifulSoup(html_text, "lxml")
    nodes = list(iter_jsonld(soup))

    canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical = clean_text(canonical_node.get("href")) if canonical_node else ""
    url = canonicalize_url(urljoin(requested_url, canonical or meta_content(soup, "og:url") or requested_url))

    title = (
        meta_content(soup, "og:title", "twitter:title")
        or clean_text(jsonld_value(nodes, "headline"))
        or clean_text(soup.h1.get_text(" ", strip=True) if soup.h1 else "")
    )
    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True)).split(" | ")[0]
    if not title:
        raise FeedError(f"No title found for {url}")

    date_value = (
        meta_content(soup, "article:published_time", "date", "datePublished")
        or clean_text(jsonld_value(nodes, "datePublished"))
    )
    page_text = soup.get_text("\n", strip=True)
    if not date_value:
        time_node = soup.find("time")
        if time_node:
            date_value = clean_text(time_node.get("datetime") or time_node.get_text(" ", strip=True))
    if not date_value:
        match = DATE_TEXT_RE.search(page_text)
        date_value = clean_text(match.group(1)) if match else ""
    published_dt = parse_date(date_value, config.get("timezone", "Europe/Vienna"))
    if published_dt is None:
        raise FeedError(f"No publication date found for {url}; raw value={date_value!r}")

    description = (
        meta_content(soup, "og:description", "twitter:description", "description")
        or clean_text(jsonld_value(nodes, "description"))
        or first_article_paragraph(soup, title)
    )
    if not plausible_description(description, title):
        description = first_article_paragraph(soup, title)
    if not description:
        description = title
    if len(description) > 700:
        description = description[:697].rsplit(" ", 1)[0] + "…"

    author = parse_author(jsonld_value(nodes, "author")) or meta_content(soup, "author")
    if not author:
        match = AUTHOR_RE.search(page_text)
        author = clean_text(match.group(1)) if match else ""
    author = author or config.get("default_author", "newsmedia.report Editorial Team")

    category = clean_text(jsonld_value(nodes, "articleSection"))
    if not category:
        match = CATEGORY_RE.search(page_text)
        category = clean_text(match.group(1)) if match else ""
    category = re.split(r"(?:Format|Author|Autor)\s*:", category, maxsplit=1, flags=re.I)[0].strip()

    html_lang = soup.html.get("lang", "") if soup.html else ""
    language = clean_text(html_lang)
    image = extract_image(soup, nodes, url)

    return Article(
        url=url,
        title=title,
        published=published_dt.astimezone(timezone.utc).isoformat(),
        description=description,
        author=author,
        category=category,
        image=image,
        language=language,
        article_id=article_id(url),
    )


def load_state(path: Path) -> dict[str, Article]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logging.warning("Ignoring unreadable state file: %s", exc)
        return {}
    result: dict[str, Article] = {}
    for entry in raw.get("articles", []):
        try:
            item = Article(**entry)
            result[canonicalize_url(item.url)] = item
        except (TypeError, ValueError):
            continue
    return result


def save_state(path: Path, articles: list[Article], keep: int = 120) -> None:
    payload = {
        "version": 1,
        "articles": [asdict(item) for item in articles[:keep]],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def collect_articles(
    session: requests.Session, urls: list[str], config: dict[str, Any]
) -> list[Article]:
    state_path = ROOT / config["state_file"]
    cache = load_state(state_path)
    refresh_newest = int(config.get("refresh_newest", 20))
    delay = float(config.get("request_delay_seconds", 0.15))
    collected: dict[str, Article] = {}

    for index, url in enumerate(urls):
        normalized = canonicalize_url(url)
        cached = cache.get(normalized)
        should_refresh = index < refresh_newest or cached is None
        if not should_refresh and cached:
            collected[normalized] = cached
            continue

        try:
            response = fetch(session, normalized)
            item = parse_article(response.text, response.url, config)
            collected[canonicalize_url(item.url)] = item
            logging.info("Parsed: %s", item.title)
        except (requests.RequestException, FeedError, ValueError) as exc:
            if cached:
                collected[normalized] = cached
                logging.warning("Using cached copy for %s: %s", normalized, exc)
            else:
                logging.warning("Skipping %s: %s", normalized, exc)
        if delay:
            time.sleep(delay)

    for url, item in cache.items():
        if url not in collected and url in urls:
            collected[url] = item

    ordered = sorted(
        collected.values(),
        key=lambda item: (item.published_dt, item.article_id),
        reverse=True,
    )
    if not ordered:
        raise FeedError("No articles could be parsed. The existing rss.xml was left untouched.")
    return ordered


def mime_type_for_image(url: str) -> str:
    guessed, _ = mimetypes.guess_type(urlparse(url).path)
    return guessed or "image/jpeg"


def item_html(article: Article) -> str:
    parts = []
    if article.image:
        parts.append(
            f'<p><a href="{html.escape(article.url, quote=True)}">'
            f'<img src="{html.escape(article.image, quote=True)}" '
            f'alt="{html.escape(article.title, quote=True)}" loading="lazy"></a></p>'
        )
    parts.append(f"<p>{html.escape(article.description)}</p>")
    parts.append(
        f'<p><a href="{html.escape(article.url, quote=True)}">Read the full article on newsmedia.report</a></p>'
    )
    return "".join(parts)


def write_feed(articles: list[Article], config: dict[str, Any]) -> Path:
    max_items = int(config["max_items"])
    items = articles[:max_items]
    nsmap = {
        "atom": NS_ATOM,
        "media": NS_MEDIA,
        "content": NS_CONTENT,
        "dc": NS_DC,
    }
    root = etree.Element("rss", version="2.0", nsmap=nsmap)
    channel = etree.SubElement(root, "channel")

    def add(parent: etree._Element, tag: str, text: str) -> etree._Element:
        element = etree.SubElement(parent, tag)
        element.text = text
        return element

    add(channel, "title", config["feed_title"])
    add(channel, "link", config["site_url"])
    add(channel, "description", config["feed_description"])
    add(channel, "language", config.get("feed_language", "en-US"))
    add(channel, "copyright", config.get("copyright", ""))
    add(channel, "generator", "newsmedia.report RSS Generator 1.0")
    add(channel, "ttl", "10")
    add(channel, "lastBuildDate", format_datetime(items[0].published_dt))
    etree.SubElement(
        channel,
        etree.QName(NS_ATOM, "link"),
        href=config["feed_url"],
        rel="self",
        type="application/rss+xml",
    )

    for article in items:
        item = etree.SubElement(channel, "item")
        add(item, "title", article.title)
        add(item, "link", article.url)
        guid = add(item, "guid", article.url)
        guid.set("isPermaLink", "true")
        add(item, "pubDate", format_datetime(article.published_dt))
        creator = etree.SubElement(item, etree.QName(NS_DC, "creator"))
        creator.text = article.author
        if article.category:
            add(item, "category", article.category)

        teaser_html = item_html(article)
        description = etree.SubElement(item, "description")
        description.text = etree.CDATA(teaser_html)
        encoded = etree.SubElement(item, etree.QName(NS_CONTENT, "encoded"))
        encoded.text = etree.CDATA(teaser_html)

        if article.image:
            mime_type = mime_type_for_image(article.image)
            etree.SubElement(
                item,
                "enclosure",
                url=article.image,
                length="0",
                type=mime_type,
            )
            etree.SubElement(
                item,
                etree.QName(NS_MEDIA, "content"),
                url=article.image,
                medium="image",
                type=mime_type,
            )
            etree.SubElement(
                item,
                etree.QName(NS_MEDIA, "thumbnail"),
                url=article.image,
            )

    output_path = ROOT / config["output_file"]
    xml_bytes = etree.tostring(
        root,
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
    )
    output_path.write_bytes(xml_bytes)
    return output_path


def validate_feed(path: Path, expected_items: int) -> None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()
    if root.tag != "rss" or root.get("version") != "2.0":
        raise FeedError("Generated file is not RSS 2.0")
    count = len(tree.xpath("/rss/channel/item"))
    if count == 0:
        raise FeedError("Generated feed contains no items")
    if count > expected_items:
        raise FeedError(f"Generated feed contains {count} items, expected at most {expected_items}")


def content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config = load_config()
        session = make_session(config)
        urls = discover_article_urls(session, config)
        if not urls:
            raise FeedError("No Webador article URLs were discovered")
        articles = collect_articles(session, urls, config)
        output_path = write_feed(articles, config)
        validate_feed(output_path, int(config["max_items"]))
        save_state(ROOT / config["state_file"], articles)
        logging.info(
            "Feed ready: %s (%d items, sha256 %s)",
            output_path.name,
            min(len(articles), int(config["max_items"])),
            content_hash(output_path),
        )
        return 0
    except Exception as exc:  # Keep the previous valid feed if generation fails.
        logging.error("Feed generation failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
