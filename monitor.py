#!/usr/bin/env python3
"""
LDJ $1 handbag watcher.

Scans every product listing on ldj.com for a $1 giveaway that has just been
added to the product DESCRIPTION (not the price). Shopify stores expose all
product data, including the full description (body_html) and a last-updated
timestamp, via a public JSON feed at /products.json.
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

STORE = "https://ldj.com"
PAGE_SIZE = 250
STATE_FILE = Path(__file__).parent / "state.json"
CHANGELOG_FILE = Path(__file__).parent / "changelog.md"
MAX_CHANGELOG_ENTRIES = 200

NTFY_TOPIC = "ldj-watch-9dc7a477"

CODE_PATTERNS = [
    re.compile(r"\b(?:code|coupon|promo)\s*[:\-]?\s*[A-Z0-9]{4,15}\b", re.IGNORECASE),
]
STANDALONE_DOLLAR_PATTERN = re.compile(r"\$1(?![\d,])")
SOFT_SIGNAL_PATTERN = re.compile(
    r"\b(?:giveaway|snatch|win this item|tag\s*@luxedujour)\b", re.IGNORECASE
)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-price-watcher/1.0)"}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_products() -> list:
    products = []
    page = 1
    while True:
        url = f"{STORE}/products.json?limit={PAGE_SIZE}&page={page}"
        try:
            data = fetch_json(url)
        except urllib.error.URLError as e:
            print(f"Fetch failed on page {page}: {e}", file=sys.stderr)
            break
        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
        time.sleep(0.5)
    return products


def find_code(text: str):
    if not text:
        return None
    for pattern in CODE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    if STANDALONE_DOLLAR_PATTERN.search(text):
        m = re.search(r".{0,25}\$1(?![\d,]).{0,50}", text, re.IGNORECASE)
        return m.group(0).strip() if m else "$1 mention"
    m = SOFT_SIGNAL_PATTERN.search(text)
    if m:
        return m.group(0)
    return None


CODE_TOKEN_PATTERN = re.compile(
    r"\b(?:code|coupon|promo)\s*[:\-]?\s*([A-Z0-9]{4,15})\b", re.IGNORECASE
)


def extract_code_token(text: str):
    if not text:
        return None
    m = CODE_TOKEN_PATTERN.search(text)
    return m.group(1).upper() if m else None


def build_quick_link(product: dict, code_token) -> str:
    variants = product.get("variants", [])
    variant_id = variants[0].get("id") if variants else None
    if not variant_id:
        handle = product.get("handle", "")
        return f"{STORE}/products/{handle}"

    cart_add_path = f"/cart/add?id={variant_id}&quantity=1"
    if code_token:
        import urllib.parse
        encoded_redirect = urllib.parse.quote(cart_add_path, safe="")
        return f"{STORE}/discount/{code_token}?redirect={encoded_redirect}"
    return f"{STORE}{cart_add_path}"


def price_is_one_dollar(product: dict) -> bool:
    for variant in product.get("variants", []):
        try:
            price = float(variant.get("price", ""))
        except (TypeError, ValueError):
            continue
        if abs(price - 1.0) < 0.01:
            return True
    return False


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def log_change(title: str, url: str, matched: bool, snippet: str) -> None:
    tag = "MATCH" if matched else "no match"
    entry = f"- **[{tag}]** {title} -- {url}\n  > {snippet[:200]}\n"
    existing = CHANGELOG_FILE.read_text().splitlines() if CHANGELOG_FILE.exists() else []
    lines = [entry] + existing
    trimmed = "\n".join(lines[:MAX_CHANGELOG_ENTRIES])
    CHANGELOG_FILE.write_text(trimmed + "\n" if trimmed else "")


def send_alert(title: str, message: str, url: str) -> None:
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Click": url,
                "Priority": "urgent",
                "Tags": "rotating_light",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy push failed: {e}", file=sys.stderr)


def main() -> None:
    state = load_state()
    seen_updated_at = state.get("updated_at", {})
    already_alerted = state.get("already_alerted", [])
    already_alerted_set = set(already_alerted)
    new_seen_updated_at = dict(seen_updated_at)

    print("Fetching full catalog...")
    products = fetch_all_products()
    print(f"Fetched {len(products)} products.")

    first_run = len(seen_updated_at) == 0
    hits = []

    for p in products:
        pid = str(p["id"])
        updated_at = p.get("updated_at", "")
        new_seen_updated_at[pid] = updated_at

        if first_run:
            continue
        if seen_updated_at.get(pid) == updated_at:
            continue

        description = p.get("body_html", "")
        code = find_code(description)
        code_token = extract_code_token(description)
        price_flag = price_is_one_dollar(p)
        signal = code or ("price listed at $1" if price_flag else None)
        handle = p.get("handle", "")
        url = f"{STORE}/products/{handle}"
        title = p.get("title", "Unknown product")
        quick_link = build_quick_link(p, code_token)

        log_change(title, url, matched=bool(signal), snippet=description or "(empty)")

        if signal and pid not in already_alerted_set:
            hits.append((pid, title, signal, url, quick_link, code_token))

    if first_run:
        print(f"First run: recorded baseline for {len(products)} products. No alerts sent.")
    elif hits:
        for pid, title, code, url, quick_link, code_token in hits:
            print(f"MATCH: {title} -- {code} -- {url}")
            code_line = f"Code: {code_token}\n" if code_token else ""
            send_alert(
                title="LDJ $1 giveaway detected!",
                message=f"{title}\nDetected: {code}\n{code_line}Quick link: {quick_link}\nProduct page: {url}",
                url=quick_link,
            )
            already_alerted_set.add(pid)
    else:
        print("No new matches this run.")

    save_state({
        "updated_at": new_seen_updated_at,
        "already_alerted": sorted(already_alerted_set),
        "last_run": time.time(),
    })


if __name__ == "__main__":
    main()
