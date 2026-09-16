#!/usr/bin/env python3
"""
Casio Bhawar Store - Deep Discount Watcher
-------------------------------------------
Polls the public Shopify product feed for the /collections/watches
collection, computes the real discount (price vs compare_at_price) for
every variant, and alerts you the first time a deal crosses
DISCOUNT_THRESHOLD percent - via a push notification (with the watch's
photo attached) and, optionally, email.

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
import json
import os
import smtplib
import time
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timedelta

# ---------------- Config ----------------
COLLECTION_URL = "https://casiostore.bhawar.com/collections/watches/products.json"
DISCOUNT_THRESHOLD = 50          # percent - change if you want a different cutoff
CHECK_INTERVAL_SECONDS = 300     # 5 minutes, used only in loop mode
SEEN_TTL_HOURS = 24              # a deal "forgotten" after this long can alert again

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
USER_AGENT = "Mozilla/5.0 (compatible; PersonalDealBot/1.0)"
REQUEST_TIMEOUT = 15
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


def fetch_products():
    """Pull every product in the collection via Shopify's public JSON feed."""
    products = []
    page = 1
    while True:
        url = f"{COLLECTION_URL}?limit=250&page={page}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"HTTP error on page {page}: {e}")
            break
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
            break

        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        page += 1
    return products


def find_deep_discounts(products, threshold):
    """Return every variant whose real discount % is >= threshold."""
    deals = []
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
            if discount_pct >= threshold:
                deals.append({
                    "id": variant.get("id"),
                    "title": title,
                 #   "variant_title": variant.get("title"),
                    "price": price,
                    "compare_at": compare_at,
                    "discount_pct": round(discount_pct, 1),
                    "url": f"https://casiostore.bhawar.com/products/{handle}?variant={variant.get('id')}",
                    "image_url": image_url,
                })
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


def run_once():
    seen = load_seen()
    products = fetch_products()
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Checked {len(products)} products")

    deals = find_deep_discounts(products, DISCOUNT_THRESHOLD)
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
    return new_deals


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
    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        main_loop()
