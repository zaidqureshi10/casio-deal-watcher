# Casio Bhawar Store — Deep Discount Watcher

Watches `casiostore.bhawar.com` for deeply discounted, **in-stock** Casio
watches and alerts you the moment one appears — with a photo, via push
notification and (optionally) email.

## How it works

1. Sweeps the public Shopify JSON feeds for `/collections/all` **plus**
   `watches`, `edifice-watches`, `g-shock`, `casio-vintage`, `casio` and
   `new-launch`, de-duplicating by product handle.
2. Computes the real discount percentage itself from `price` vs.
   `compare_at_price`, rather than relying on the site's own "% off" tag.
   (The collection *pages* render `MRP ₹ … (0% Off)` on every card because
   the theme shows the product-level price range, not the variant sale
   price — the JSON feed is the accurate source.)
3. Skips any variant marked `"available": false` — sold-out watches are
   filtered out so you're only pinged about deals you can actually buy.
4. The first time a specific variant+price crosses the threshold, it sends
   a push notification (with the watch's photo attached) via
   [ntfy](https://ntfy.sh), and an email if you've configured SMTP.
   Deals are processed highest-discount-first.
5. Remembers each alerted deal in `seen_deals.json` for **24 hours**
   (`SEEN_TTL_HOURS`) so you don't get repeat alerts for the same still-live
   deal — but if it's still discounted after 24h, or the discount reappears
   later, you'll be notified again rather than it going silent forever.

## Checking what's on sale right now

```bash
python3 watch_alert.py --dry-run
```

Lists every current in-stock deal without sending notifications or touching
`seen_deals.json`. Useful for sanity-checking the threshold before you let
it loose.

## Requirements

- Python 3.9+ — no external packages, standard library only
- The [ntfy](https://ntfy.sh) app on your phone (free, no signup)
- (Optional) a customer account on the store — `SHOP_EMAIL` / `SHOP_PASSWORD`
  make requests with a logged-in session. Not required; the public feed
  already carries the sale prices.
- (Optional) an SMTP account for email alerts — e.g. a Gmail address with
  an [app password](https://myaccount.google.com/apppasswords)

## Not getting notifications?

Run the built-in self-test first — it sends one fake alert:

```bash
python3 watch_alert.py --test-notify
```

If your phone stays silent, work through these in order:

1. **`NTFY_TOPIC` not set.** The most common cause. Without it the script
   falls back to a placeholder topic that isn't yours (and which ntfy may
   reject with `HTTP Error 403`). Add `NTFY_TOPIC=your-topic` to `.env` and
   as a GitHub secret, and subscribe to that exact topic in the ntfy app.
2. **No baseline yet.** Tracked items can't alert on the *first* run —
   there's nothing to compare against. If every run prints `baseline`, no
   baseline is being saved; see below.
3. **You only ever ran `--dry-run`.** A dry run deliberately writes no
   state, so the baseline is never stored and the next run starts over.
   Run once *without* `--dry-run` to seed `tracked_prices.json`.
4. **On GitHub Actions: the state commit is failing.** If the "Save
   seen-deals state" step errors, `tracked_prices.json` never persists
   between runs, so every run re-records a baseline and nothing ever
   alerts. Check that step's log.

## Watching specific Amazon / Flipkart items

The same run also checks a list of individual product URLs and pings you on
**any** price drop, however small. Put them in `tracked_items.json`:

```json
[
  "https://www.amazon.in/dp/B0XXXXXXXX",
  {
    "url": "https://www.flipkart.com/some-watch/p/itmXXXXXXXX",
    "label": "Casio Duro MDV-106",
    "target_price": 3000
  }
]
```

- A bare string is fine; the object form adds an optional `label` (friendly
  name in alerts) and `target_price` (also alert any time it's at or below
  this, even without a fresh drop).
- Strings starting with `#` are ignored, so you can comment items out.
- Or add one from the CLI: `python3 item_watch.py --add "<url>" --label "Name"`

The last seen price per URL is kept in `tracked_prices.json`. The first run
only records a baseline — alerts start from the second run. The baseline
follows the price up as well as down, so a dip after a price rise still
counts as a drop.

### Re-alerting policy (tracked items)

Each check compares the new price only against the **last price stored for
that URL** — not the original baseline, and there's no cooldown/TTL like the
Casio watcher has:

- new price `<` last stored price → **alerts**, then stores the new price
- new price `==` last stored price → no alert
- new price `>` last stored price → no alert, but the higher price is still
  stored (so a later dip counts as a drop even after a price hike)

This means a item can alert on every single run it finds a lower figure than
last time, e.g. ₹8,200 → ₹8,000 → ₹7,900 all fire, even within the same day.
If that's too noisy, raise the bar by setting a `target_price` on the item
instead of (or alongside) relying on any-drop alerting.

### Getting a confirmation alert immediately (`ITEM_ALERT_ON_BASELINE`)

By default the very first time an item is seen it only records a baseline
and does **not** notify — there's nothing to compare against yet. To prove
the alert path works end-to-end (e.g. right after adding new items, or on a
fresh GitHub Actions setup with no `tracked_prices.json` committed yet), set:

```
ITEM_ALERT_ON_BASELINE=true
```

in `.env`, or as shown already enabled in `.github/workflows/deal-check.yml`.
With it on, a brand-new item sends one "Tracking started: …" notification on
its first run, then falls back to normal drop-only alerting afterwards.
Leave it `false`/unset once you've confirmed delivery, if you'd rather not
get a notification every time you add a new item to track.

```bash
python3 item_watch.py --dry-run            # just the tracked items
python3 watch_alert.py --once              # Casio sweep + tracked items
python3 watch_alert.py --once --no-items   # Casio sweep only
python3 watch_alert.py --items-only        # tracked items only
```

### Seeing *your* logged-in price

Flipkart and Amazon personalise prices — e.g. an account-specific "₹408 off"
offer that drops the Nimbus 27 from ₹8,159 to ₹7,751. That discount is **not
in the logged-out HTML**, so by default the watcher tracks the public price.

To track your own price, give it your browser's session cookie:

1. Open the product page in your browser, logged in.
2. DevTools (F12) → **Network** tab.
3. **For Amazon, check "Disable cache" first, then hard-reload** with
   Ctrl+Shift+R (not a normal reload). Amazon's document request is often
   served from cache on a plain reload, which hides the `Cookie:` header
   entirely in DevTools. Flipkart usually doesn't need this, but it doesn't
   hurt to do it there too.
4. Click the **topmost request** — the HTML document, Type `document`,
   usually named after the product slug.
5. Under **Request Headers**, find the `Cookie:` row → right-click →
   **Copy value**. Copy the *entire* line, not individual cookies: the
   personalised price depends on several of them together (session + auth
   token + account id), so cherry-picking one name (e.g. just `SN` or `at`)
   won't work.

   > Don't use Application → Cookies for this — it lists cookies one per row
   > and you'd have to reassemble them into `name=value; name=value; …`
   > yourself. The Network tab gives you that string ready-made.

   If the `document` request still doesn't show a `Cookie:` header after
   step 3, try one of these instead:
   - Switch the Network filter to **Fetch/XHR**, interact with the page
     (scroll, open a dropdown) to trigger a background request, and copy
     the `Cookie:` header from that request instead — it's the same cookie
     jar.
   - Open the **Console** tab and run `document.cookie`. This is quick but
     **only returns non-HttpOnly cookies** — Amazon keeps its most
     important session cookies (`session-id`, `at-acbin`, `session-token`)
     `HttpOnly`, so this method alone usually isn't enough for Amazon.

6. Put it on **one line** in `.env` (or a GitHub secret of the same name):

```
FLIPKART_COOKIE=K-ACTION=...; T=...; SN=...
AMAZON_COOKIE=session-id=...; x-main=...; at-acbin=...
```

Then verify it's actually being honoured:

```bash
python3 item_watch.py --compare-cookie
```

This fetches each item twice — once anonymously, once with your cookie — and
prints both prices side by side:

```
      public    logged-in   item
   Rs.8,159     Rs.7,751    Asics Gel Nimbus 27 (men)  <- cookie working, personalised price found
```

If both columns match on a product you know is personalised, the cookie has
expired or too few cookies were copied.

The normal run also prints `(using logged-in cookies: FLIPKART_COOKIE)` when
one is active. Caveats: these cookies **are** your login — treat them like
passwords, never commit them (`.env` is gitignored). They expire every few
weeks, so re-copy when prices suddenly revert to the public figure.

### Caveat: Amazon blocks bots

Amazon frequently serves a captcha page to datacenter IPs — which is exactly
what GitHub Actions runs on. When that happens you'll see
`BOT CHECK` in the log and that item is skipped (its stored price is left
untouched, so no false "drop" alert later). Flipkart is usually more
tolerant. If Amazon blocks you persistently, run the watcher from your own
machine or a home server instead of Actions, where it works reliably.

Note this is independent of the `AMAZON_COOKIE` above — the captcha is
triggered by Amazon's IP/bot-reputation checks, not by being logged in or
out. A valid cookie gets you the logged-in price *when Amazon lets the
request through*; it won't stop the captcha from appearing on a flagged IP.

Price extraction tries JSON-LD structured data first, then OpenGraph/microdata
meta tags, then Amazon/Flipkart-specific patterns — so most other shops work
out of the box too.

## Running locally

Create a `.env` file next to `watch_alert.py` (already gitignored, never
commit it):

```
NTFY_TOPIC=pick-something-long-and-random

# Optional - browse with a logged-in session (not needed for prices)
SHOP_EMAIL=you@example.com
SHOP_PASSWORD=your_store_password

# Optional - track your personalised Flipkart/Amazon prices (see above)
FLIPKART_COOKIE=
AMAZON_COOKIE=

# Optional - alert once on a tracked item's first-ever check too (see above)
ITEM_ALERT_ON_BASELINE=false

# Optional - only add these if you also want email alerts
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your_16_char_app_password
EMAIL_TO=you@gmail.com
```

ntfy topics are public — anyone who guesses your topic name can see your
alerts, so make it long and random. Subscribe to that topic in the ntfy
app, then run:

```bash
python3 watch_alert.py            # loops forever, checks every 5 min
python3 watch_alert.py --once     # single check, useful for cron
```

Keep it running in the background with `nohup python3 watch_alert.py &`,
a `screen`/`tmux` session, or a systemd user service / Task Scheduler entry.

## Running for free on GitHub Actions (no laptop needed)

1. Push this folder to a new **public** GitHub repo (public = unlimited
   free Actions minutes; a private repo works too but gets 2,000 free
   minutes/month, so widen the cron interval to ~30 min to stay under that).
2. Repo → **Settings → Secrets and variables → Actions → New repository
   secret** → add `NTFY_TOPIC`, `SHOP_EMAIL` and `SHOP_PASSWORD`. Add the five
   `SMTP_*` / `EMAIL_TO` secrets too if you want email alerts (leave them
   unset to skip email entirely).
3. Repo → **Settings → Actions → General → Workflow permissions** → select
   **Read and write permissions** → Save. (Lets the workflow commit updated
   `seen_deals.json` after each run.)
4. Repo → **Actions** tab → select "Casio deal check" → **Run workflow** to
   test it manually. Check the log for `Checked N unique products`.
5. From then on it runs automatically every 15 minutes, for free.

## Email setup notes (optional)

- **Gmail**: turn on 2-Step Verification, then create an
  [app password](https://myaccount.google.com/apppasswords) — use that as
  `SMTP_PASS`, not your normal password. `SMTP_HOST=smtp.gmail.com`,
  `SMTP_PORT=587`.
- Any other provider's SMTP works too — just fill in its host/port/creds.
- If `SMTP_HOST` and `EMAIL_TO` aren't set, email is skipped automatically
  and only the ntfy push notification is sent.

## Configuration

Edit the constants near the top of `watch_alert.py`:

| Variable                 | Default | Meaning                                          |
|---------------------------|---------|---------------------------------------------------|
| `DISCOUNT_THRESHOLD`      | `70`    | Minimum % off to trigger an alert                 |
| `CHECK_INTERVAL_SECONDS`  | `300`   | Poll interval in local loop mode                  |
| `SEEN_TTL_HOURS`          | `24`    | How long a deal is "remembered" before re-alerting |

The GitHub Actions cron schedule lives in
`.github/workflows/deal-check.yml` (`*/15 * * * *`).

## A few notes

- This store's `robots.txt` requests no automated crawling. This script is
  meant for personal, low-frequency use (one request every 15+ minutes) —
  not for scraping the whole catalog rapidly or redistributing data.
- Your ntfy topic and any SMTP credentials are secrets — keep them in
  `.env` / GitHub secrets, never commit them.
- `seen_deals.json` is intentionally tracked in git (not ignored) so state
  survives between GitHub Actions runs; expired entries are pruned from it
  automatically on each run.
