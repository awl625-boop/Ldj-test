#!/usr/bin/env python3
"""
LDJ $1 handbag watcher.
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

STORE = "https://ldj.com"
PAGE_SIZE = 250
STATE_FILE = Path(__file__).parent / "state.json"
CHANGELOG_FILE = Path(__file__).parent / "changelog.md"
MAX_CHANGELOG_ENTRIES = 200
MAX_NUDGE_BURST = 15  # more photo-changes than this in one run = likely a
                       # bulk reshoot/restock, not individual giveaways
MAX_ANOMALY_CHECKS_PER_RUN = 30  # cap full-page fetches for ID/serial check

NTFY_TOPIC = "ldj-watch-9dc7a477"

CODE_PATTERNS = [
    re.compile(r"\b(?:code|coupon|promo)\s*[:\-]?\s*[A-Z0-9]{4,15}\b", re.IGNORECASE),
]

PRICE_PATTERNS = [
    re.compile(r"\$1(?![\d,])"),
    re.compile(r"\$1\.00\b"),
    re.compile(r"\bone\s+dollar\b", re.IGNORECASE),
    re.compile(r"\b1\s+buck\b", re.IGNORECASE),
    re.compile(r"\bpenny\s+deal\b", re.IGNORECASE),
    re.compile(r"\bone\s+cent\b", re.IGNORECASE),
]

SOFT_SIGNAL_PATTERN = re.compile(
    r"\b(?:giveaway|snatch|win this item|tag\s*@luxedujour)\b",
    re.IGNORECASE
)

# Fuzzy/typo variations -- LOG-ONLY, never triggers an alert. In case LDJ
# ever uses deliberate misspellings to dodge detection (unconfirmed).
FUZZY_SIGNAL_PATTERN = re.compile(
    r"\b(?:g[i1]v[e3]aw[a4]y[a4]?|sn[a4]tch|fr[e3][e3]\s+(?:item|bag|handbag)|"
    r"z[e3]r[o0]\s*c[o0]st|t[a4]g\s*@|r[a4]ffl[e3])\b",
    re.IGNORECASE
)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-price-watcher/1.0)"}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip_html_tags(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    for pattern in PRICE_PATTERNS:
        m = pattern.search(text)
        if m:
            snippet = re.search(r".{0,25}" + pattern.pattern + r".{0,50}", text, re.IGNORECASE)
            return snippet.group(0).strip() if snippet else "Price alert: $1 or less"
    m = SOFT_SIGNAL_PATTERN.search(text)
    if m:
        return m.group(0)
    return None


def find_fuzzy_signal(text: str):
    """Log-only check -- never used for alerting, only for changelog review."""
    if not text:
        return None
    m = FUZZY_SIGNAL_PATTERN.search(text)
    return m.group(0) if m else None


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


def get_main_image_url(product: dict):
    images = product.get("images", [])
    if images:
        return images[0].get("src")
    return None


def get_image_signature(product: dict):
    images = product.get("images", [])
    return sorted(str(img.get("id", img.get("src", ""))) for img in images)


# ID is normally purely numeric (e.g. "1109023"). Serial date code is
# normally digits with trailing X's used to mask the real value (e.g.
# "2491XXXX") -- any letter OTHER than X in that field is unexpected.
ID_FIELD_PATTERN = re.compile(r"\bID\s*:\s*([A-Za-z0-9\-]+)", re.IGNORECASE)
SERIAL_FIELD_PATTERN = re.compile(r"Serial date code\s*:\s*([A-Za-z0-9\-]+)", re.IGNORECASE)


def check_id_serial_anomaly(url: str):
    """Fetch the FULL product page (not just the lightweight JSON feed) and
    check the ID / Serial date code fields for unexpected letters -- a
    possible spot for a scrambled/unscramble-style code. Only called for
    listings that already changed this run and had no other signal, to
    avoid fetching all 3000+ full pages every cycle.

    Best-effort: the exact page markup isn't something we've verified
    directly, so this may need tuning if it turns out too noisy or misses
    real cases -- treat hits as "worth a look," not certainty."""
    try:
        html = fetch_text(url)
    except Exception as e:
        print(f"Could not fetch page for anomaly check: {e}", file=sys.stderr)
        return None

    text = strip_html_tags(html)
    findings = []

    id_match = ID_FIELD_PATTERN.search(text)
    if id_match:
        id_value = id_match.group(1)
        if re.search(r"[A-Za-z]", id_value):
            findings.append(f"ID field has letters: {id_value}")

    serial_match = SERIAL_FIELD_PATTERN.search(text)
    if serial_match:
        serial_value = serial_match.group(1)
        if re.search(r"[A-WYZa-wyz]", serial_value):  # any letter except X/x
            findings.append(f"Serial code has unusual letters: {serial_value}")

    return "; ".join(findings) if findings else None


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def log_change(title: str, url: str, tag: str, snippet: str, created_at: str = "") -> None:
    created_line = f" (listed: {created_at})" if created_at else " (listed: unknown)"
    entry = f"- **[{tag}]** {title}{created_line} -- {url}\n  > {snippet[:200]}\n"
    existing = CHANGELOG_FILE.read_text().splitlines() if CHANGELOG_FILE.exists() else []
    lines = [entry] + existing
    trimmed = "\n".join(lines[:MAX_CHANGELOG_ENTRIES])
    CHANGELOG_FILE.write_text(trimmed + "\n" if trimmed else "")


def send_alert(title: str, message: str, url: str, image_url=None) -> None:
    try:
        headers = {
            "Title": title,
            "Click": url,
            "Priority": "urgent",
            "Tags": "rotating_light",
        }
        if image_url:
            headers["Attach"] = image_url
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy push failed: {e}", file=sys.stderr)


def send_nudge_digest(nudge_list: list) -> None:
    if not nudge_list:
        return
    count = len(nudge_list)

    if count > MAX_NUDGE_BURST:
        body = (
            f"{count} listings had new photos in this run -- likely a bulk "
            f"reshoot or restock rather than individual giveaways, so not "
            f"listing them all here. Full list is in changelog.md if you "
            f"want to check."
        )
        title = f"LDJ: {count} listings changed photos (bulk update, not itemized)"
    else:
        lines = [f"- {t}\n  {u}" for t, u in nudge_list]
        body = "\n".join(lines)
        title = f"LDJ: {count} listing(s) got new photos, no text match"

    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "default",
                "Tags": "eyes",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy nudge digest push failed: {e}", file=sys.stderr)


def send_anomaly_alert(title: str, url: str, finding: str) -> None:
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=f"{title}\n{finding}\n{url}".encode("utf-8"),
            headers={
                "Title": "LDJ: unusual ID/serial code field",
                "Click": url,
                "Priority": "default",
                "Tags": "mag",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy anomaly push failed: {e}", file=sys.stderr)


def main() -> None:
    state = load_state()
    seen_updated_at = state.get("updated_at", {})
    already_alerted = state.get("already_alerted", [])
    already_alerted_set = set(already_alerted)
    already_nudged = state.get("already_nudged", [])
    already_nudged_set = set(already_nudged)
    already_anomaly_checked = state.get("already_anomaly_checked", [])
    already_anomaly_checked_set = set(already_anomaly_checked)
    seen_image_sig = state.get("image_sig", {})
    new_seen_updated_at = dict(seen_updated_at)
    new_seen_image_sig = dict(seen_image_sig)

    print("Fetching full catalog...")
    products = fetch_all_products()
    print(f"Fetched {len(products)} products.")

    first_run = len(seen_updated_at) == 0
    hits = []
    nudges = []
    anomaly_checks_done = 0

    for p in products:
        pid = str(p["id"])
        updated_at = p.get("updated_at", "")
        image_sig = get_image_signature(p)
        new_seen_updated_at[pid] = updated_at
        new_seen_image_sig[pid] = image_sig

        if first_run:
            continue
        if seen_updated_at.get(pid) == updated_at:
            continue

        description = p.get("body_html", "")
        code = find_code(description)
        code_token = extract_code_token(description)
        price_flag = price_is_one_dollar(p)
        signal = code or ("price listed at $1" if price_flag else None)
        fuzzy = find_fuzzy_signal(description)
        handle = p.get("handle", "")
        url = f"{STORE}/products/{handle}"
        title = p.get("title", "Unknown product")
        quick_link = build_quick_link(p, code_token)
        image_url = get_main_image_url(p)
        images_changed = seen_image_sig.get(pid, image_sig) != image_sig
        created_at = p.get("created_at", "")
        is_existing_listing = pid in seen_updated_at

        if signal:
            log_change(title, url, "MATCH", description or "(empty)", created_at)
        elif fuzzy:
            log_change(title, url, "fuzzy", f"matched '{fuzzy}' -- {description[:150] or '(empty)'}", created_at)
        elif images_changed and is_existing_listing:
            log_change(title, url, "photo-edit on existing listing", description or "(empty)", created_at)
        else:
            log_change(title, url, "no match", description or "(empty)", created_at)

        if signal and pid not in already_alerted_set:
            hits.append((pid, title, signal, url, quick_link, code_token, image_url))
        elif not signal and images_changed and is_existing_listing and pid not in already_nudged_set:
            nudges.append((pid, title, url))

        # ID/Serial anomaly check -- only for changed listings with no
        # other signal, capped per run to avoid excessive full-page fetches.
        if (not signal and not fuzzy and is_existing_listing
                and pid not in already_anomaly_checked_set
                and anomaly_checks_done < MAX_ANOMALY_CHECKS_PER_RUN):
            anomaly_checks_done += 1
            already_anomaly_checked_set.add(pid)
            finding = check_id_serial_anomaly(url)
            if finding:
                print(f"ANOMALY: {title} -- {finding}")
                log_change(title, url, "ID/serial anomaly", finding, created_at)
                send_anomaly_alert(title, url, finding)

    if first_run:
        print(f"First run: recorded baseline for {len(products)} products. No alerts sent.")
    else:
        for pid, title, code, url, quick_link, code_token, image_url in hits:
            print(f"MATCH: {title} -- {code} -- {url}")
            code_line = f"Code: {code_token}\n" if code_token else ""
            send_alert(
                title="LDJ $1 giveaway detected!",
                message=f"{title}\nDetected: {code}\n{code_line}Quick link: {quick_link}\nProduct page: {url}",
                url=quick_link,
                image_url=image_url,
            )
            already_alerted_set.add(pid)

        if nudges:
            print(f"NUDGE DIGEST: {len(nudges)} listing(s) with new photos, no text match")
            send_nudge_digest([(title, url) for pid, title, url in nudges])
            for pid, title, url in nudges:
                already_nudged_set.add(pid)

        if not hits and not nudges and anomaly_checks_done == 0:
            print("No new matches, nudges, or anomaly checks this run.")

    save_state({
        "updated_at": new_seen_updated_at,
        "image_sig": new_seen_image_sig,
        "already_alerted": sorted(already_alerted_set),
        "already_nudged": sorted(already_nudged_set),
        "already_anomaly_checked": sorted(already_anomaly_checked_set),
        "last_run": time.time(),
    })


if __name__ == "__main__":
    main()
 
