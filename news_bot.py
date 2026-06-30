#!/usr/bin/env python3
"""news_bot.py

Real-time Ethiopian News Aggregator & Telegram Publisher

Single-file script implementing:
- RSS ingestion via `feedparser`
- Scraping fallback image extraction via requests + BeautifulSoup
- SQLite tracking DB to avoid duplicates
- Translation to Amharic using `deep-translator` (GoogleTranslator)
- Telegram publishing with robust URL-then-upload fallback
- Daemon (polling) and single-run modes

Configuration is at the top in `SOURCES` and environment variables.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import logging
import mimetypes
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import feedparser
    import requests
    from bs4 import BeautifulSoup
except Exception as exc:  # pragma: no cover - environment deps
    print(
        "Missing runtime dependency. Install requirements: feedparser, requests, beautifulsoup4",
        file=sys.stderr,
    )
    raise

try:
    from deep_translator import GoogleTranslator
except Exception:  # pragma: no cover - optional
    GoogleTranslator = None

try:
    from langdetect import detect
except Exception:  # pragma: no cover - optional
    detect = None

# load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

LOG = logging.getLogger("news_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Resilient shared session with retries
SESSION = requests.Session()
RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
BACKOFF = float(os.getenv("HTTP_BACKOFF", "1"))
retry_strategy = Retry(
    total=RETRIES,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
    backoff_factor=BACKOFF,
)
adapter = HTTPAdapter(max_retries=retry_strategy)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

# Central configuration: edit sources here.
SOURCES: Dict[str, str] = {
    "Fana": "https://fanabc.com/feed/",  # adjust if needed
    "Addis Standard": "https://addisstandard.com/feed/",
    "Ethiopian News Agency": "https://www.ena.et/feed/",
}

# Placeholder image when no image is resolved
PLACEHOLDER_IMAGE = os.getenv("PLACEHOLDER_IMAGE", "https://picsum.photos/1200/800")

# Database file
DB_PATH = os.getenv("NEWS_DB", "news_tracker.db")

# Telegram config from environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # channel id or @channelusername

# Polling interval (seconds)
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", str(5 * 60)))

# Rate-limit between Telegram posts
POST_DELAY = float(os.getenv("POST_DELAY", "3"))


@dataclass
class Article:
    source: str
    url: str
    title: str
    summary: str
    published: Optional[datetime]
    image_url: Optional[str]


def ensure_db(path: str = DB_PATH) -> None:
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS processed (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            title TEXT,
            published TIMESTAMP
        )
        """
        )
        conn.commit()
    finally:
        conn.close()


def seen_before(url: str, path: str = DB_PATH) -> bool:
    uid = hashlib.md5(url.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM processed WHERE id = ?", (uid,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def mark_seen(url: str, title: str, published: Optional[datetime], path: str = DB_PATH) -> None:
    uid = hashlib.md5(url.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO processed (id, url, title, published) VALUES (?, ?, ?, ?)",
            (uid, url, title, published.isoformat() if published else None),
        )
        conn.commit()
    finally:
        conn.close()


def safe_determine_published(entry) -> Optional[datetime]:
    # Try common feed fields
    for key in ("published_parsed", "updated_parsed", "published", "updated"):
        v = entry.get(key)
        if v:
            try:
                if key.endswith("_parsed"):
                    return datetime.fromtimestamp(time.mktime(v))
                else:
                    return datetime.fromisoformat(v)
            except Exception:
                try:
                    # fallback to feedparser parse
                    return datetime(*entry.get("published_parsed")[:6])
                except Exception:
                    continue
    return None


def extract_image_from_entry(entry) -> Optional[str]:
    # Common RSS enclosure
    try:
        media = entry.get("media_content") or entry.get("media_thumbnail")
        if media:
            if isinstance(media, list):
                for m in media:
                    url = m.get("url") if isinstance(m, dict) else None
                    if url:
                        return url
            elif isinstance(media, dict):
                return media.get("url")
    except Exception:
        pass

    # Links with rel=enclosure
    links = entry.get("links", []) or []
    for link in links:
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image"):
            return link.get("href")

    # Some feeds include it directly
    for key in ("image", "thumbnail", "enclosure"):
        v = entry.get(key)
        if isinstance(v, dict) and v.get("url"):
            return v.get("url")

    return None


def fetch_article_page(url: str, timeout: int = 10) -> Optional[str]:
    headers = {"User-Agent": "news-bot/1.0 (+https://example.org)"}
    try:
        r = SESSION.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as exc:
        LOG.debug("Failed to fetch article page %s: %s", url, exc)
        return None


def find_best_image_in_html(html_text: str, base_url: str = "") -> Optional[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    # Open Graph
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"]
    # Twitter
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return tw["content"]

    # First large-looking image in article
    selectors = [
        "article img",
        ".post-thumbnail img",
        ".featured img",
        ".entry-content img",
    ]
    candidates: List[Tuple[int, str]] = []
    for sel in selectors:
        for img in soup.select(sel):
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            width = 0
            try:
                width = int(img.get("width") or 0)
            except Exception:
                width = 0
            candidates.append((width, src))
    if candidates:
        # prefer largest width
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # Any image fallback
    imgs = soup.find_all("img")
    for img in imgs:
        src = img.get("src") or img.get("data-src")
        if src and "sprite" not in src and src.strip():
            return src

    return None


def resolve_image(entry, article_url: str) -> str:
    # 1) try RSS metadata
    img = extract_image_from_entry(entry)
    if img:
        return img

    # 2) try scraping
    page = fetch_article_page(article_url)
    if page:
        page_img = find_best_image_in_html(page, base_url=article_url)
        if page_img:
            return page_img

    # 3) fallback to placeholder
    return PLACEHOLDER_IMAGE


def detect_language(text: str) -> Optional[str]:
    if detect is None:
        return None
    try:
        return detect(text)
    except Exception:
        return None


def translate_to_amharic(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    if GoogleTranslator is None:
        LOG.warning("Translation requested but deep-translator is not installed. Returning original text.")
        return text
    try:
        translator = GoogleTranslator(source="auto", target="am")
        return translator.translate(text)
    except Exception as exc:
        LOG.warning("Translation failed: %s", exc)
        return text


def build_caption(title: str, summary: str, article_url: str, source_name: str) -> str:
    # sanitize
    title_s = html.unescape(title or "")
    summary_s = html.unescape(summary or "")
    # escape for HTML except we will add our tags
    title_esc = html.escape(title_s)
    summary_esc = html.escape(summary_s)

    header = f"🚨 <b>{title_esc}</b>\n"
    cta = f"\n🔗 <a href=\"{html.escape(article_url)}\">ሙሉውን መረጃ ለማንበብ እዚህ ይጫኑ</a>\n"
    src = f"\n📍 ምንጭ: {html.escape(source_name)}"

    # combine and ensure under 1024 characters
    remaining_limit = 1024 - (len(header) + len(cta) + len(src))
    if remaining_limit < 0:
        # extremely unlikely, but guard
        header = header[:900]
        remaining_limit = 1024 - (len(header) + len(cta) + len(src))

    if len(summary_esc) > remaining_limit:
        summary_esc = summary_esc[: max(0, remaining_limit - 3)] + "..."

    caption = f"{header}{summary_esc}{cta}{src}"
    if len(caption) > 1024:
        caption = caption[:1023]
    return caption


def post_to_telegram(image_url: str, caption: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        LOG.error("Telegram credentials not set. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False

    api_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    send_photo = f"{api_base}/sendPhoto"

    payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": caption, "parse_mode": "HTML"}
    try:
        r = SESSION.post(send_photo, data=payload, timeout=15)
        if r.ok and r.json().get("ok"):
            LOG.info("Posted via URL successfully")
            return True
        LOG.debug("Telegram send via URL failed: %s", r.text)
    except Exception as exc:
        LOG.debug("Telegram send via URL exception: %s", exc)

    # fallback: download image and upload as file
    try:
        headers = {"User-Agent": "news-bot/1.0 (+https://example.org)"}
        img_resp = SESSION.get(image_url, headers=headers, stream=True, timeout=20)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get("Content-Type") or "image/jpeg"
        ext = mimetypes.guess_extension(content_type.split(";")[0]) or ".jpg"
        bio = io.BytesIO(img_resp.content)
        bio.name = f"upload{ext}"
        files = {"photo": (bio.name, bio, content_type)}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        r2 = requests.post(send_photo, data=data, files=files, timeout=30)
        if r2.ok and r2.json().get("ok"):
            LOG.info("Posted via upload successfully")
            return True
        LOG.error("Telegram upload failed: %s", r2.text)
        return False
    except Exception as exc:
        LOG.exception("Failed to upload image to Telegram: %s", exc)
        return False


def process_feed(source_name: str, feed_url: str) -> None:
    LOG.info("Fetching feed %s (%s)", source_name, feed_url)
    try:
        feed = feedparser.parse(feed_url)
    except Exception as exc:
        LOG.error("Failed to parse feed %s: %s", feed_url, exc)
        return

    entries = feed.entries or []
    # Reverse-processing order: oldest -> newest
    def entry_key(e):
        dt = safe_determine_published(e)
        return dt or datetime.fromtimestamp(0)

    entries_sorted = sorted(entries, key=entry_key)

    for entry in entries_sorted:
        link = entry.get("link") or entry.get("id")
        if not link:
            LOG.debug("Skipping entry without link: %s", entry.get("title"))
            continue

        if seen_before(link):
            LOG.debug("Already processed: %s", link)
            continue

        title = entry.get("title") or "(No title)"
        summary = entry.get("summary") or entry.get("description") or ""
        published = safe_determine_published(entry)

        # Clean HTML entities
        title = html.unescape(title)
        summary = html.unescape(BeautifulSoup(summary, "html.parser").get_text())

        # Language detection and translation
        lang = detect_language(title + "\n" + summary) or "unknown"
        if lang != "am":
            LOG.debug("Translating from %s to amharic", lang)
            title_translated = translate_to_amharic(title)
            summary_translated = translate_to_amharic(summary)
        else:
            title_translated = title
            summary_translated = summary

        # Resolve image
        image_url = resolve_image(entry, link)

        article = Article(
            source=source_name,
            url=link,
            title=title_translated,
            summary=summary_translated,
            published=published,
            image_url=image_url,
        )

        caption = build_caption(article.title, article.summary, article.url, article.source)

        try:
            ok = post_to_telegram(article.image_url or PLACEHOLDER_IMAGE, caption)
        except Exception:
            LOG.exception("Unhandled exception while posting to Telegram for %s", article.url)
            ok = False

        if ok:
            mark_seen(article.url, article.title, article.published)
            LOG.info("Processed and posted: %s", article.url)
            time.sleep(POST_DELAY)
        else:
            LOG.warning("Failed to post article: %s", article.url)


def run_once() -> None:
    ensure_db()
    for source_name, feed_url in SOURCES.items():
        try:
            process_feed(source_name, feed_url)
        except Exception:
            LOG.exception("Error processing source %s", source_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ethiopian News Aggregator & Telegram Bot")
    parser.add_argument("--single-run", action="store_true", dest="single_run", help="Run once and exit")
    args = parser.parse_args()

    single_run = args.single_run or os.getenv("SINGLE_RUN") == "1"

    LOG.info("Starting news_bot (single_run=%s)", single_run)

    if single_run:
        run_once()
        LOG.info("Single run complete")
        return

    try:
        while True:
            run_once()
            LOG.info("Sleeping for %s seconds...", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        LOG.info("Interrupted by user, exiting.")


if __name__ == "__main__":
    main()
