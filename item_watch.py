#!/usr/bin/env python3
"""
Specific-item price watcher (Amazon / Flipkart / most shops)
------------------------------------------------------------
Give it a list of product URLs in `tracked_items.json` and it will alert you
whenever the price drops by ANY amount versus the last price it saw.

How it differs from the Casio watcher in watch_alert.py:
  * watch_alert.py  -> "find me anything >= X% off across a whole store"
  * item_watch.py   -> "tell me the moment THIS exact product gets cheaper"

State lives in `tracked_prices.json` (last seen price per URL). The first
run just records a baseline and doesn't alert; every run after that
compares against that baseline. The baseline follows the price in both
directions, so a drop from a *raised* price still counts as a drop.

Price extraction is layered, most reliable first:
  1. JSON-LD  (<script type="application/ld+json"> ... "offers":{"price":...})
  2. OpenGraph / microdata meta tags (og:price:amount, itemprop="price")
  3. Site-specific patterns for Amazon and Flipkart
No third-party packages - stdlib only.

Usage:
    python3 item_watch.py --once       # one check (cron / GitHub Actions)
    python3 item_watch.py --dry-run    # show current prices, don't alert or save
    python3 item_watch.py --add URL    # append a URL to tracked_items.json
"""

import argparse
import gzip
import html as html_module
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path

ITEMS_FILE = Path(__file__).with_name("tracked_items.json")
STATE_FILE = Path(__file__).with_name("tracked_prices.json")

REQUEST_TIMEOUT = 40
RETRIES = 3
DELAY_BETWEEN_ITEMS = 4      # seconds - be polite, and avoid tripping bot defences

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
}

# Optional per-site cookies, so we can see *your* logged-in price rather than
# the public one. Flipkart/Amazon personalise prices (e.g. a "Rs 408 off"
# offer that only applies to your account), and those discounts simply are
# not present in the logged-out HTML.
#
# Set them as environment variables / GitHub secrets holding a raw Cookie
# header string copied from your browser:
#     FLIPKART_COOKIE="K-ACTION=...; T=...; SN=..."
#     AMAZON_COOKIE="session-id=...; x-main=...; at-acbin=..."
# See the README for how to copy one. Leave unset to use public prices.
COOKIE_ENV_BY_DOMAIN = {
    "flipkart.": "FLIPKART_COOKIE",
    "amazon.": "AMAZON_COOKIE",
    "casiostore.bhawar.com": "CASIO_COOKIE",
}


def cookie_for(url):
    """The Cookie header to send for this URL, if one is configured."""
    host = urllib.parse.urlparse(url).netloc.lower()
    for fragment, env_var in COOKIE_ENV_BY_DOMAIN.items():
        if fragment in host:
            value = os.environ.get(env_var)
            if value:
                return _clean_cookie(value)
    return None


def _clean_cookie(value):
    """
    Be forgiving about however the cookie got pasted in: DevTools' "Copy
    value" is already clean, but people often include the "Cookie:" prefix,
    wrap it in quotes, or let it span several lines.
    """
    value = value.strip().strip('"').strip("'")
    value = re.sub(r"^cookie\s*:\s*", "", value, flags=re.I)
    # Collapse any newlines/tabs a multi-line paste introduced.
    value = re.sub(r"\s*[\r\n\t]+\s*", " ", value)
    return value.strip().rstrip(";")


# ---------------------------------------------------------------- fetching

class BotCheck(Exception):
    """The site served a captcha / "are you a robot" interstitial."""


BOT_MARKERS = (
    "/errors/validatecaptcha",
    "enter the characters you see below",
    "type the characters you see in this image",
    "to discuss automated access to amazon data",
    "are you a robot",
    "px-captcha",
    "cf-challenge",
)


def looks_like_bot_check(html):
    low = html.lower()
    return len(html) < 20000 and any(marker in low for marker in BOT_MARKERS)


def fetch_html(url):
    """GET a product page as a browser would, decompressing if needed."""
    headers = dict(BROWSER_HEADERS)
    cookie = cookie_for(url)
    if cookie:
        headers["Cookie"] = cookie

    last_error = None
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            elif encoding == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            html = raw.decode("utf-8", "ignore")
            if looks_like_bot_check(html):
                raise BotCheck("served a bot-check page instead of the product")
            return html
        except BotCheck as e:
            last_error = e
            if attempt < RETRIES:
                time.sleep(5 * attempt)
        except urllib.error.HTTPError as e:
            # 404 usually means a bad/expired URL - no point retrying.
            if e.code in (403, 404, 410):
                raise
            last_error = e
            if attempt < RETRIES:
                time.sleep(2 * attempt)
        except Exception as e:
            last_error = e
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise last_error


# ---------------------------------------------------------------- parsing

_NUM = r"\d[\d,\u00a0 ]*(?:\.\d{1,2})?"


def _to_float(text):
    if text is None:
        return None
    text = str(text).replace("\u00a0", "").replace(",", "").replace(" ", "").strip()
    text = re.sub(r"^(?:₹|Rs\.?|INR|\$)", "", text, flags=re.I)
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _walk_for_price(node):
    """Depth-first search through decoded JSON-LD for an offers price."""
    if isinstance(node, dict):
        if "price" in node:
            value = _to_float(node.get("price"))
            if value:
                return value
        for key in ("offers", "Offers", "lowPrice", "priceSpecification"):
            if key in node:
                found = _walk_for_price(node[key])
                if found:
                    return found
        for value in node.values():
            found = _walk_for_price(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _walk_for_price(item)
            if found:
                return found
    return None


def price_from_jsonld(html):
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        block = match.group(1).strip()
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        price = _walk_for_price(data)
        if price:
            return price
    return None


def price_from_meta(html):
    patterns = [
        r'<meta[^>]+(?:property|name)=["\'](?:og:price:amount|product:price:amount|twitter:data1)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:price:amount|product:price:amount)["\']',
        r'<[^>]+itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            price = _to_float(match.group(1))
            if price:
                return price
    return None


def price_from_amazon(html):
    patterns = [
        # The buybox price, split across whole/fraction spans
        r'<span class="a-price aok-align-center[^"]*"[^>]*>\s*<span class="a-offscreen">\s*₹?\s*(' + _NUM + r')',
        r'id="corePriceDisplay[^"]*".{0,2000}?<span class="a-price-whole">\s*(' + _NUM + r')',
        r'"priceAmount"\s*:\s*(' + _NUM + r')',
        r'id="priceblock_(?:ourprice|dealprice|saleprice)"[^>]*>\s*₹?\s*(' + _NUM + r')',
        r'<span class="a-offscreen">\s*₹\s*(' + _NUM + r')\s*</span>',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.S | re.I)
        if match:
            price = _to_float(match.group(1))
            if price:
                return price
    return None


def price_from_flipkart(html):
    patterns = [
        # The product price block looks like:
        #   "ppd":{"fsp":7751,"finalPrice":8159,"mrp":16999,...}
        # fsp = "final selling price" - the figure you actually pay, with any
        # account-specific offer already applied. NOTE "finalPrice" is a trap:
        # despite the name it holds the *public* price, so fsp must win.
        r'"ppd"\s*:\s*\{[^{}]*?"fsp"\s*:\s*(' + _NUM + r')',
        r'"fsp"\s*:\s*(' + _NUM + r')',
        # Same number again inside the analytics blob, as a fallback.
        r'\\"fktp\\"\s*:\s*(' + _NUM + r')',
        # Logged-out / older layouts.
        r'"(?:selling_price|final_price)"\s*:\s*\{[^}]*?"value"\s*:\s*(' + _NUM + r')',
        r'"finalPrice"\s*:\s*(' + _NUM + r')',
        r'"price"\s*:\s*\{\s*"value"\s*:\s*(' + _NUM + r')',
        # Flipkart mangles its CSS class names every few weeks, so these are
        # a last resort only.
        r'class="[^"]*\bNx9bqj\b[^"]*"[^>]*>\s*₹(' + _NUM + r')',
        r'class="[^"]*\b_30jeq3\b[^"]*"[^>]*>\s*₹(' + _NUM + r')',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.S | re.I)
        if match:
            price = _to_float(match.group(1))
            if price:
                return price
    return None


def title_from_html(html, url):
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<span[^>]+id=["\']productTitle["\'][^>]*>(.*?)</span>',
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, html, re.S | re.I)
        if match:
            title = html_module.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
            if title:
                return title[:140]
    return url


def image_from_html(html):
    match = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    return html_module.unescape(match.group(1)) if match else None


def extract_price(html, url):
    """Best-effort price for any shop, with Amazon/Flipkart specialisation."""
    host = urllib.parse.urlparse(url).netloc.lower()

    extractors = [price_from_jsonld, price_from_meta]
    if "amazon." in host:
        extractors.insert(0, price_from_amazon)
    elif "flipkart." in host:
        extractors.insert(0, price_from_flipkart)

    for extractor in extractors:
        try:
            price = extractor(html)
        except Exception:
            continue
        if price:
            return price
    return None


# ---------------------------------------------------------------- config / state

def load_items():
    """
    tracked_items.json is a list of either plain URL strings or objects:
        ["https://www.amazon.in/dp/XXXX",
         {"url": "https://www.flipkart.com/...", "label": "Casio Duro",
          "target_price": 3000}]
    `label` is cosmetic; `target_price` (optional) means "also alert me any
    time it's at or below this, even without a fresh drop".
    """
    if not ITEMS_FILE.exists():
        return []
    try:
        raw = json.loads(ITEMS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"tracked_items.json is not valid JSON: {e}")
        return []

    items = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        url = entry["url"].strip()
        if url.startswith("#") or not url.lower().startswith("http"):
            continue    # lets you "comment out" an item
        items.append({
            "url": url,
            "label": entry.get("label"),
            "target_price": entry.get("target_price"),
        })
    return items


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def add_item(url, label=None):
    existing = json.loads(ITEMS_FILE.read_text(encoding="utf-8")) if ITEMS_FILE.exists() else []
    urls = {e if isinstance(e, str) else e.get("url") for e in existing}
    if url in urls:
        print("Already tracked.")
        return
    existing.append({"url": url, "label": label} if label else url)
    ITEMS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"Added. Now tracking {len(existing)} item(s).")


# ---------------------------------------------------------------- the check

def check_items(notify, dry_run=False):
    """
    `notify` is a callable taking a deal dict - watch_alert.notify_deal is
    passed in, so item alerts reuse the exact same ntfy + email plumbing.
    """
    items = load_items()
    if not items:
        print("No items in tracked_items.json - nothing to watch.")
        return 0

    state = load_state()
    alerts = 0
    print(f"Checking {len(items)} tracked item(s)...")

    configured = sorted({
        env for env in COOKIE_ENV_BY_DOMAIN.values() if os.environ.get(env)
    })
    if configured:
        print(f"  (using logged-in cookies: {', '.join(configured)})")

    for index, item in enumerate(items):
        url = item["url"]
        if index:
            time.sleep(DELAY_BETWEEN_ITEMS)

        try:
            html = fetch_html(url)
        except BotCheck:
            print(f"  BOT CHECK     {item['label'] or url}\n"
                  f"                (site demanded a captcha - common for Amazon from "
                  f"cloud/CI IPs; price left unchanged)")
            continue
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code}      {item['label'] or url}"
                  + ("  <- check the URL is still valid" if e.code == 404 else ""))
            continue
        except Exception as e:
            print(f"  FETCH FAILED  {item['label'] or url}: {e}")
            continue

        price = extract_price(html, url)
        title = item["label"] or title_from_html(html, url)
        if price is None:
            print(f"  NO PRICE FOUND  {title} "
                  f"(page may be a bot check, or out of stock)")
            continue

        previous = state.get(url, {}).get("price")
        target = item.get("target_price")

        if previous is None:
            print(f"  baseline  Rs.{price:>10,.0f}  {title}")
        elif price < previous:
            drop = previous - price
            pct = drop / previous * 100
            print(f"  DROP      Rs.{price:>10,.0f}  (-Rs.{drop:,.0f}, -{pct:.1f}%)  {title}")
            if not dry_run:
                notify({
                    "id": url,
                    "title": title,
                    "price": price,
                    "compare_at": previous,
                    "discount_pct": round(pct, 1),
                    "url": url,
                    "image_url": image_from_html(html),
                })
            alerts += 1
        elif target and price <= target:
            print(f"  AT TARGET Rs.{price:>10,.0f}  (target Rs.{target:,.0f})  {title}")
            if not dry_run:
                notify({
                    "id": url,
                    "title": title,
                    "price": price,
                    "compare_at": max(previous, target),
                    "discount_pct": 0.0,
                    "url": url,
                    "image_url": image_from_html(html),
                })
            alerts += 1
        elif price > previous:
            print(f"  up        Rs.{price:>10,.0f}  (was Rs.{previous:,.0f})  {title}")
        else:
            print(f"  same      Rs.{price:>10,.0f}  {title}")

        # Track the latest price in both directions so a later dip off a
        # raised price still registers as a drop.
        state[url] = {
            "price": price,
            "title": title,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    if dry_run:
        print("Dry run - no notifications sent, state not written.")
    else:
        save_state(state)
    return alerts


def compare_cookie():
    """
    Fetch every tracked item twice - once anonymously, once with the
    configured cookie - and show both prices. The quickest way to tell
    whether your cookie is actually being honoured: if the two columns are
    identical for a product you know is personalised, the cookie isn't
    working (expired, or too few cookies copied).
    """
    items = load_items()
    if not items:
        print("No items in tracked_items.json.")
        return

    print(f"{'public':>12} {'logged-in':>12}   item")
    for index, item in enumerate(items):
        url = item["url"]
        if index:
            time.sleep(DELAY_BETWEEN_ITEMS)

        cookie = cookie_for(url)
        if not cookie:
            host = urllib.parse.urlparse(url).netloc
            print(f"{'-':>12} {'no cookie':>12}   {item['label'] or host}")
            continue

        prices = {}
        for mode, use_cookie in (("public", False), ("logged-in", True)):
            headers = dict(BROWSER_HEADERS)
            if use_cookie:
                headers["Cookie"] = cookie
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    raw = resp.read()
                    encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                elif encoding == "deflate":
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                html = raw.decode("utf-8", "ignore")
                prices[mode] = None if looks_like_bot_check(html) else extract_price(html, url)
            except Exception:
                prices[mode] = None
            time.sleep(2)

        def fmt(value):
            return f"Rs.{value:,.0f}" if value else "?"

        flag = ""
        if prices["public"] and prices["logged-in"]:
            if prices["logged-in"] < prices["public"]:
                flag = "  <- cookie working, personalised price found"
            else:
                flag = "  <- same; cookie may be expired or unnecessary"
        print(f"{fmt(prices['public']):>12} {fmt(prices['logged-in']):>12}   "
              f"{item['label'] or url}{flag}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Specific-item price watcher")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show current prices without alerting or saving state")
    parser.add_argument("--add", metavar="URL", help="Add a product URL to tracked_items.json")
    parser.add_argument("--label", help="Optional friendly name to use with --add")
    parser.add_argument("--compare-cookie", action="store_true",
                        help="Fetch each item with and without your cookie to verify it works")
    args = parser.parse_args()

    if args.add:
        add_item(args.add, args.label)
    else:
        import watch_alert
        watch_alert.load_local_env()
        if args.compare_cookie:
            compare_cookie()
        else:
            check_items(watch_alert.notify_deal, dry_run=args.dry_run)
