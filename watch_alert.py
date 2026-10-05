#!/usr/bin/env python3
"""
Casio Bhawar Store - Deep Discount Watcher
-------------------------------------------
Scans the store's public Shopify product feeds, computes the real discount
(price vs compare_at_price) for every variant, and alerts you the first
time an **in-stock** deal crosses DISCOUNT_THRESHOLD percent - via a push
notification (with the watch's photo attached) and, optionally, email.
Sold-out variants are skipped.

Important: /collections/watches/products.json only exposes ~408 products,
while the store actually sells ~1,360. Lots of discounted models (e.g.
MTP-VT01G-9B at 50% off) live outside that collection, which is why
watching a single collection missed them. We therefore sweep
/collections/all plus every named collection below and de-duplicate by
product handle.

Prices in the feed are already the real sale prices (the ones shown on
the product page), so no login is required. SHOP_EMAIL / SHOP_PASSWORD
remain optional: if set, requests are made with a logged-in session in
case any member-only pricing ever shows up.

A given deal (product + price) won't re-alert for SEEN_TTL_HOURS, after
which it "forgets" it and will alert again if the discount is still live -
so a recurring flash sale keeps notifying you each time it reappears
instead of going silent forever after the first hit.

No third-party packages required - stdlib only.

Usage:
    python3 watch_alert.py           # run forever, checking on an interval
    python3 watch_alert.py --once    # check a single time and exit (cron / GitHub Actions)
"""

import argparse
import http.cookiejar
import json
import os
import smtplib
import time
import urllib.parse
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timedelta

# ---------------- Config ----------------
STORE = "https://casiostore.bhawar.com"
LOGIN_URL = f"{STORE}/account/login"

# "all" is the catch-all collection and on its own covers every product the
# store sells; the rest are kept as a safety net in case "all" is ever
# restricted or paginated differently. Results are de-duplicated by handle.
COLLECTIONS = [
    "all",
    "watches",
    "edifice-watches",
    "g-shock",
    "casio-vintage",
    "casio",
    "new-launch",
]

DISCOUNT_THRESHOLD = 50          # percent - change if you want a different cutoff
CHECK_INTERVAL_SECONDS = 300     # 5 minutes, used only in loop mode
SEEN_TTL_HOURS = 24              # a deal "forgotten" after this long can alert again
MAX_COLLECTION_PAGES = 40        # safety stop when paging a collection feed

# Store login - optional. The public feed already carries the sale prices,
# but logging in makes the session behave like a real shopper.
SHOP_EMAIL = os.environ.get("SHOP_EMAIL")
SHOP_PASSWORD = os.environ.get("SHOP_PASSWORD")

# ntfy (push notifications) - reads NTFY_TOPIC from env / GitHub secret / .env
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "casio-deals-CHANGE-ME")


# Email (optional) - only used if SMTP_HOST and EMAIL_TO are both set.
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_ENABLED = bool(SMTP_HOST and EMAIL_TO)

STATE_FILE = Path(__file__).with_name("seen_deals.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
REQUEST_TIMEOUT = 40
# -----------------------------------------


def load_local_env():
    """
    Tiny, dependency-free .env loader for local runs. If a .env file sits
    next to this script, load KEY=VALUE lines into os.environ (without
    overwriting variables already set in the real environment). Ignored
    on GitHub Actions, where secrets arrive as real env vars.
    """
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def make_session():
    """A urllib opener that keeps cookies, so we stay logged in."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        ("Accept", "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    return opener


def get_text(opener, url, data=None, referer=None):
    headers = {"Referer": referer} if referer else {}
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers)
    with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "ignore"), resp.geturl()


def login(opener):
    """
    Log into the Shopify storefront as a customer. The store only renders the
    real sale prices (e.g. 70% Special Offer) for logged-in customers - logged
    out, every product shows MRP with "(0% Off)", which is why the public
    products.json feed never reports a discount.

    Returns True if the session looks authenticated.
    """
    if not (SHOP_EMAIL and SHOP_PASSWORD):
        print("SHOP_EMAIL / SHOP_PASSWORD not set - browsing as a guest "
              "(the public feed still carries the sale prices).")
        return False

    try:
        get_text(opener, LOGIN_URL)  # pick up session + CSRF cookies
        _, final_url = get_text(
            opener,
            LOGIN_URL,
            data={
                "form_type": "customer_login",
                "utf8": "✓",
                "customer[email]": SHOP_EMAIL,
                "customer[password]": SHOP_PASSWORD,
                "return_url": "/account",
            },
            referer=LOGIN_URL,
        )
        account_html, account_url = get_text(opener, f"{STORE}/account")
    except Exception as e:
        print(f"Login failed: {e}")
        return False

    logged_in = "/account/login" not in account_url and (
        "logout" in account_html.lower() or "order history" in account_html.lower()
    )
    print("Logged in as a customer." if logged_in
          else f"Login did not take (landed on {final_url}). Check SHOP_EMAIL/SHOP_PASSWORD.")
    return logged_in


def fetch_collection(opener, collection):
    """Every product in one collection, via Shopify's paginated JSON feed."""
    products = []
    page = 1
    while page <= MAX_COLLECTION_PAGES:
        url = f"{STORE}/collections/{collection}/products.json?limit=250&page={page}"
        try:
            raw, _ = get_text(opener, url)
            batch = json.loads(raw).get("products", [])
        except urllib.error.HTTPError as e:
            print(f"  {collection}: HTTP error on page {page}: {e}")
            break
        except Exception as e:
            print(f"  {collection}: error on page {page}: {e}")
            break
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        page += 1
    return products


def fetch_products(opener):
    """
    Sweep every configured collection and de-duplicate by handle.

    /collections/watches alone exposes only a fraction of the catalogue, so
    discounted models that aren't tagged into it (MTP-VT01G-9B, for example)
    were silently invisible. /collections/all covers everything; the rest are
    belt-and-braces.
    """
    by_handle = {}
    for collection in COLLECTIONS:
        batch = fetch_collection(opener, collection)
        new = 0
        for product in batch:
            handle = product.get("handle")
            if handle and handle not in by_handle:
                by_handle[handle] = product
                new += 1
        print(f"  {collection}: {len(batch)} products ({new} new)")
    return list(by_handle.values())


def find_deep_discounts(products, threshold):
    """
    Return every *in-stock* variant whose real discount % is >= threshold.

    Shopify's feed marks each variant with "available": true/false, which is
    false once inventory runs out (the product page then shows "Sold out" and
    the Add to cart button is disabled). There's no point being pinged about a
    70% off watch you can't actually buy, so those are skipped.
    """
    deals = []
    skipped_sold_out = 0
    for product in products:
        title = product.get("title", "Unknown")
        handle = product.get("handle", "")
        images = product.get("images") or []
        image_url = images[0]["src"] if images and images[0].get("src") else None

        for variant in product.get("variants", []):
            try:
                price = float(variant.get("price") or 0)
                compare_at = float(variant.get("compare_at_price") or 0)
            except (TypeError, ValueError):
                continue

            if compare_at <= 0 or price <= 0 or price >= compare_at:
                continue

            discount_pct = (compare_at - price) / compare_at * 100
            if discount_pct < threshold:
                continue

            # Treat a missing "available" key as in stock - better a rare
            # false alarm than silently dropping a real deal.
            if variant.get("available") is False:
                skipped_sold_out += 1
                continue

            deals.append({
                "id": variant.get("id"),
                "title": title,
                "price": price,
                "compare_at": compare_at,
                "discount_pct": round(discount_pct, 1),
                "url": f"{STORE}/products/{handle}?variant={variant.get('id')}",
                "image_url": image_url,
            })

    if skipped_sold_out:
        print(f"Skipped {skipped_sold_out} discounted but sold-out variant(s)")
    deals.sort(key=lambda d: d["discount_pct"], reverse=True)
    return deals


def load_seen():
    """Load seen deals, dropping any entry older than SEEN_TTL_HOURS so it
    can alert again if the discount is still (or newly) live."""
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}

    cutoff = datetime.now() - timedelta(hours=SEEN_TTL_HOURS)
    fresh = {}
    for key, entry in raw.items():
        seen_at_str = entry.get("seen_at") if isinstance(entry, dict) else None
        try:
            seen_at = datetime.fromisoformat(seen_at_str) if seen_at_str else None
        except ValueError:
            seen_at = None
        if seen_at is None or seen_at < cutoff:
            continue  # expired (or malformed/legacy entry) - treat as forgotten
        fresh[key] = entry
    return fresh


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen, indent=2))


def send_ntfy(deal):
    message = (
        f"{deal['title']} \n"
        f"{deal['discount_pct']}% off - Rs.{deal['price']:.0f} (was Rs.{deal['compare_at']:.0f})\n"
        f"{deal['url']}"
    )
    headers = {
        "Title": f"Casio deal: {deal['discount_pct']}% off",
        "Priority": "high",
        "Tags": "watch,moneybag",
        "Click": deal["url"],
    }
    if deal.get("image_url"):
        headers["Attach"] = deal["image_url"]  # ntfy fetches this URL and shows it as a photo

    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Failed to send ntfy notification: {e}")


def send_email(deal):
    if not EMAIL_ENABLED:
        return
    subject = f"Casio deal: {deal['discount_pct']}% off {deal['title']}"
    image_html = (
        f'<p><img src="{deal["image_url"]}" alt="watch photo" style="max-width:400px;"></p>'
        if deal.get("image_url") else ""
    )
    html = f"""
    <html><body>
      <h2>{deal['title']}</h2>
      {image_html}
      <p><b>{deal['discount_pct']}% off</b> — Rs.{deal['price']:.0f}
         <s>Rs.{deal['compare_at']:.0f}</s></p>
      <p><a href="{deal['url']}">View watch on the store</a></p>
    </body></html>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER or EMAIL_TO
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(msg["From"], [EMAIL_TO], msg.as_string())
    except Exception as e:
        print(f"Failed to send email: {e}")


def notify_deal(deal):
    send_ntfy(deal)
    send_email(deal)
    print(f"Notified: {deal['title']} - {deal['discount_pct']}% off")


def test_notify():
    """Send one fake alert, so you can prove the phone actually buzzes."""
    if NTFY_TOPIC == "casio-deals-CHANGE-ME":
        print("NTFY_TOPIC is not set! Alerts are going to the default public "
              "topic, not yours - that's why your phone is silent.\n"
              "Add NTFY_TOPIC=<your-topic> to .env (and as a GitHub secret).")
        return
    print(f"Sending a test alert to ntfy topic '{NTFY_TOPIC}'"
          + (f" and email to {EMAIL_TO}" if EMAIL_ENABLED else " (email disabled)"))
    notify_deal({
        "id": "test",
        "title": "TEST ALERT - if you can read this, notifications work",
        "price": 1234,
        "compare_at": 2468,
        "discount_pct": 50.0,
        "url": "https://example.com",
        "image_url": None,
    })


def run_once(dry_run=False, skip_items=False):
    seen = load_seen()
    opener = make_session()
    login(opener)

    products = fetch_products(opener)
    deals = find_deep_discounts(products, DISCOUNT_THRESHOLD)
    print(f"[{datetime.now().isoformat(timespec='seconds')}] "
          f"Checked {len(products)} unique products - "
          f"{len(deals)} variant(s) at >= {DISCOUNT_THRESHOLD}% off")

    if dry_run:
        for deal in deals:
            print(f"  {deal['discount_pct']:>5}% off  Rs.{deal['price']:>9,.0f} "
                  f"(was Rs.{deal['compare_at']:>9,.0f})  {deal['title']}")
        print("Dry run - no notifications sent, state not written.")
    else:
        new_deals = 0
        for deal in deals:
            key = f"{deal['id']}:{deal['price']}"
            if key not in seen:
                notify_deal(deal)
                seen[key] = {**deal, "seen_at": datetime.now().isoformat()}
                new_deals += 1

        if new_deals == 0:
            print("No new deals above threshold.")

        save_seen(seen)  # always save, so expired entries actually get pruned from disk

    if not skip_items:
        run_item_watch(dry_run=dry_run)

    return len(deals) if dry_run else new_deals


def run_item_watch(dry_run=False):
    """
    Second half of a run: check the specific Amazon/Flipkart product URLs in
    tracked_items.json and alert on ANY price drop. Kept in its own module so
    a failure there (bot-check page, site redesign) can never stop the Casio
    sweep from reporting.
    """
    print("\n--- Tracked items ---")

    items_file = Path(__file__).with_name("tracked_items.json")
    module_file = Path(__file__).with_name("item_watch.py")
    if not module_file.exists():
        print("item_watch.py is MISSING from this directory - tracked items "
              "cannot be checked.\nIf you're on GitHub Actions, make sure "
              "item_watch.py was committed/uploaded to the repo.")
        return
    if not items_file.exists():
        print("tracked_items.json is MISSING from this directory - nothing to "
              "watch.\nIf you're on GitHub Actions, make sure "
              "tracked_items.json was committed/uploaded to the repo.")
        return

    try:
        import item_watch
    except Exception as e:
        print(f"Could not import item_watch.py: {e}")
        return
    try:
        item_watch.check_items(notify_deal, dry_run=dry_run)
    except Exception as e:
        print(f"Item watch failed: {e}")


def main_loop():
    print(f"Watching casiostore.bhawar.com for >= {DISCOUNT_THRESHOLD}% discounts...")
    print(f"Checking every {CHECK_INTERVAL_SECONDS // 60} minute(s). Ctrl+C to stop.")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Unexpected error: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    load_local_env()

    parser = argparse.ArgumentParser(description="Casio Bhawar Store discount watcher")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="List every current deal without notifying or touching state")
    parser.add_argument("--no-items", action="store_true",
                        help="Skip the tracked_items.json Amazon/Flipkart check")
    parser.add_argument("--items-only", action="store_true",
                        help="Only check tracked_items.json, skip the Casio store sweep")
    parser.add_argument("--test-notify", action="store_true",
                        help="Send one fake alert to prove notifications are wired up")
    args = parser.parse_args()

    if args.test_notify:
        test_notify()
    elif args.items_only:
        run_item_watch(dry_run=args.dry_run)
    elif args.dry_run:
        run_once(dry_run=True, skip_items=args.no_items)
    elif args.once:
        run_once(skip_items=args.no_items)
    else:
        main_loop()
