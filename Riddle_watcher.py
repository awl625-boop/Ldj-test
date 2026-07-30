#!/usr/bin/env python3
"""
LDJ site-structure watcher.

Two independent checks, sharing the same sitemap-based mechanism:

1. RIDDLE HUNT (time-boxed) -- watches every static page and blog post
   (About Us, FAQs, Terms, blog articles) for recent changes, since a
   hidden riddle likely lives on one of these. Stops running after
   RIDDLE_HUNT_DEADLINE.

2. NEW COLLECTION WATCHER (permanent, no deadline) -- watches for any
   brand-new collection (category page) appearing on the site, e.g.
   "Under $5K". Useful indefinitely, not just during this giveaway.

Both rely on Shopify's public sitemap.xml, which lists every page/blog/
collection along with a <lastmod> timestamp -- the same trick monitor.py
uses for products, applied to site structure instead.

What this CAN do: point you at a page/collection that just changed or
appeared. What this CANNOT do: read a riddle for you, solve it, or guess
a code. It also won't catch anything hidden outside the sitemap (theme
files, images, unlinked pages, social media).
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

STORE = "https://ldj.com"
RIDDLE_STATE_FILE = Path(__file__).parent / "riddle_state.json"
COLLECTION_STATE_FILE = Path(__file__).parent / "collection_state.json"
NTFY_TOPIC = "ldj-watch-9dc7a477"

# Stop the RIDDLE HUNT portion after this date. The new-collection watcher
# below is NOT affected by this -- it runs forever.
RIDDLE_HUNT_DEADLINE = datetime(2026, 8, 1, 7, 0, 0, tzinfo=timezone.utc)  # ~midnight PST Jul 31

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-site-watcher/1.0)"}

RIDDLE_KEYWORDS = re.compile(
    r"\b(?:riddle|clue|solve|decode|discount code|hidden|puzzle|"
    r"unscramble|anagram|scrambled)\b", re.IGNORECASE
)

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def get_sitemap_urls(keywords: list) -> list:
    """Get (url, lastmod) pairs from any sub-sitemap whose URL contains
    one of the given keywords (e.g. "pages", "blog", "collections")."""
    try:
        index_xml = fetch_text(f"{STORE}/sitemap.xml")
        root = ET.fromstring(index_xml)
    except Exception as e:
        print(f"Failed to fetch/parse sitemap index: {e}", file=sys.stderr)
        return []

    sub_sitemaps = [
        loc.text for loc in root.findall(".//sm:sitemap/sm:loc", NS)
        if loc.text and any(k in loc.text for k in keywords)
    ]

    all_urls = []
    for sm_url in sub_sitemaps:
        try:
            sm_xml = fetch_text(sm_url)
            sm_root = ET.fromstring(sm_xml)
        except Exception as e:
            print(f"Failed to fetch/parse sub-sitemap {sm_url}: {e}", file=sys.stderr)
            continue
        for url_el in sm_root.findall(".//sm:url", NS):
            loc = url_el.find("sm:loc", NS)
            lastmod = url_el.find("sm:lastmod", NS)
            if loc is not None and loc.text:
                all_urls.append((loc.text, lastmod.text if lastmod is not None else ""))
        time.sleep(0.3)

    return all_urls


def strip_html_tags(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def send_alert(title: str, message: str, url: str, tag: str = "detective") -> None:
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Click": url,
                "Priority": "urgent",
                "Tags": tag,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy push failed: {e}", file=sys.stderr)


def run_riddle_hunt() -> None:
    """Time-boxed: watches pages/blogs for changes, looking for riddle
    clues. Stops doing anything past RIDDLE_HUNT_DEADLINE."""
    now = datetime.now(timezone.utc)
    if now > RIDDLE_HUNT_DEADLINE:
        print("Riddle hunt window has passed -- skipping.")
        return

    state = load_state(RIDDLE_STATE_FILE)
    seen_lastmod = state.get("lastmod", {})
    new_seen_lastmod = dict(seen_lastmod)

    print("Fetching site pages/blog sitemap...")
    urls = get_sitemap_urls(["pages", "blog"])
    print(f"Found {len(urls)} page/blog URLs to check.")

    first_run = len(seen_lastmod) == 0
    changed = []

    for url, lastmod in urls:
        new_seen_lastmod[url] = lastmod
        if first_run:
            continue
        if seen_lastmod.get(url) == lastmod:
            continue
        changed.append(url)

    if first_run:
        print(f"Riddle hunt first run: recorded baseline for {len(urls)} pages.")
        save_state(RIDDLE_STATE_FILE, {"lastmod": new_seen_lastmod, "last_run": time.time()})
        return

    if not changed:
        print("No page/blog changes this run.")
    else:
        for url in changed:
            print(f"CHANGED PAGE: {url}")
            try:
                html = fetch_text(url)
                text = strip_html_tags(html)
            except Exception as e:
                text = ""
                print(f"Could not fetch page content: {e}", file=sys.stderr)

            keyword_hit = RIDDLE_KEYWORDS.search(text)
            snippet = ""
            if keyword_hit:
                start = max(0, keyword_hit.start() - 100)
                end = min(len(text), keyword_hit.end() + 200)
                snippet = text[start:end]

            title = "LDJ riddle hunt: page changed (keyword match!)" if keyword_hit \
                else "LDJ riddle hunt: page changed (check manually)"
            body = f"{url}\n\n{snippet}" if snippet else f"{url}\n\nNo obvious keyword match -- worth a manual look."
            send_alert(title=title, message=body, url=url)

    save_state(RIDDLE_STATE_FILE, {"lastmod": new_seen_lastmod, "last_run": time.time()})


def run_collection_watcher() -> None:
    """Permanent, no deadline: alerts the first time a brand-new
    collection (category page) appears on the site."""
    state = load_state(COLLECTION_STATE_FILE)
    seen_lastmod = state.get("lastmod", {})
    new_seen_lastmod = dict(seen_lastmod)

    print("Fetching site collections sitemap...")
    urls = get_sitemap_urls(["collections"])
    print(f"Found {len(urls)} collection URLs to check.")

    first_run = len(seen_lastmod) == 0
    new_collections = []

    for url, lastmod in urls:
        is_new = url not in seen_lastmod
        new_seen_lastmod[url] = lastmod
        if first_run:
            continue
        if is_new:
            new_collections.append(url)

    if first_run:
        print(f"Collection watcher first run: recorded baseline for {len(urls)} collections.")
        save_state(COLLECTION_STATE_FILE, {"lastmod": new_seen_lastmod, "last_run": time.time()})
        return

    if not new_collections:
        print("No new collections this run.")
    else:
        for url in new_collections:
            print(f"NEW COLLECTION: {url}")
            title = url
            try:
                html = fetch_text(url)
                page_title = extract_title(html)
                if page_title:
                    title = page_title
            except Exception as e:
                print(f"Could not fetch collection page: {e}", file=sys.stderr)

            send_alert(
                title="LDJ: new collection created",
                message=f"{title}\n{url}",
                url=url,
                tag="new",
            )

    save_state(COLLECTION_STATE_FILE, {"lastmod": new_seen_lastmod, "last_run": time.time()})


def main() -> None:
    run_riddle_hunt()
    run_collection_watcher()


if __name__ == "__main__":
    main()
