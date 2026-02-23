import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent / "config.json"

# ---------------------------------------------------------------------------
# Per-site stock detection
# ---------------------------------------------------------------------------
SITES = {
    "argos": {
        "name": "Argos",
        # Specific button selector — most reliable signal (from streetmerchant)
        "in_stock_selectors": ['button[data-test="add-to-trolley-button-button"]'],
        # Text found on page when IN stock (fallback)
        "in_stock_text": ["add to trolley", "add to basket"],
        # Text found on page when OUT of stock
        "oos_text": ["out of stock", "sold out", "check back soon", "currently unavailable"],
        # CSS selectors for OOS elements
        "oos_selectors": [".pdp-description-info__out-of-stock"],
    },
    "smyths": {
        "name": "Smyths Toys",
        # Green button only present when item is actually purchasable
        "in_stock_selectors": ["button.bg-green-400"],
        # JSON-LD schema in page source is the most reliable signal
        "in_stock_text": ["schema.org/instock"],
        # Disabled/greyed button = OOS; red "Out of stock" span also appears
        "oos_selectors": ["button.cursor-not-allowed", "span.text-red-400"],
        "oos_text": ["schema.org/outofstock", "out of stock", "sold out"],
    },
    "game": {
        "name": "GAME",
        "in_stock_text": ["add to basket", "buy now"],
        "oos_text": ["out of stock", "sold out"],
        "oos_selectors": [".out-of-stock"],
    },
    "very": {
        "name": "Very",
        "in_stock_text": ["add to basket", "buy now"],
        "oos_text": ["out of stock", "sold out", "temporarily unavailable"],
        "oos_selectors": [".out-of-stock"],
    },
    "pokemoncenter": {
        "name": "Pokemon Centre",
        "in_stock_text": ["add to cart", "add to bag"],
        "oos_text": ["out of stock", "sold out", "notify me when available"],
        "oos_selectors": [".out-of-stock", ".sold-out"],
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def detect_site(url: str) -> str:
    url_lower = url.lower()
    if "argos.co.uk" in url_lower:
        return "argos"
    if "smythstoys.com" in url_lower:
        return "smyths"
    if "game.co.uk" in url_lower:
        return "game"
    if "very.co.uk" in url_lower:
        return "very"
    if "pokemoncenter.com" in url_lower:
        return "pokemoncenter"
    return "generic"


def send_discord(webhook_url: str, item: dict, site_name: str) -> None:
    payload = {
        "embeds": [
            {
                "title": f"✅ IN STOCK: {item['name']}",
                "url": item["url"],
                "description": (
                    f"**{item['name']}** is now in stock on **{site_name}**!\n\n"
                    f"[🛒 Click here to buy →]({item['url']})"
                ),
                "color": 0x00C851,
                "footer": {
                    "text": f"Stock Bot • {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                },
            }
        ]
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        log.info(f"  -> Discord alert sent!")
    except Exception as exc:
        log.error(f"  -> Discord notification failed: {exc}")


# ---------------------------------------------------------------------------
# Stock checking (Playwright — full JS rendering)
# ---------------------------------------------------------------------------

BLOCK_PHRASES = [
    "captcha", "are you a robot", "verify you are human", "access denied",
    "403 forbidden", "bot detected", "unusual traffic", "please verify",
    "security check", "cloudflare", "just a moment", "checking your browser",
    "enable javascript and cookies", "ray id",
]

def is_blocked(content_lower: str) -> bool:
    return any(phrase in content_lower for phrase in BLOCK_PHRASES)


def send_discord_warning(webhook_url: str, site_name: str, url: str) -> None:
    payload = {
        "embeds": [
            {
                "title": f"⚠️ BLOCKED: {site_name}",
                "url": url,
                "description": (
                    f"**{site_name}** appears to be blocking the stock checker!\n\n"
                    f"A CAPTCHA or access denied page was detected.\n"
                    f"Stock checks for this site may not be reliable until resolved."
                ),
                "color": 0xFF4500,
                "footer": {
                    "text": f"Stock Bot • {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                },
            }
        ]
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        log.warning(f"  -> Block alert sent to Discord!")
    except Exception as exc:
        log.error(f"  -> Failed to send block alert: {exc}")


def check_smyths_api(sku: str) -> bool:
    """Check Smyths stock via their internal inventory API — no browser needed."""
    url = (
        f"https://www.smythstoys.com/api/uk/en-gb/product/product-inventory"
        f"?code={sku}&userId=anonymous&bundle=false"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.smythstoys.com/uk/en-gb/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        log.info(f"  -> Smyths API HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            hd_status = data.get("hdSection", {}).get("stockStatus", "UNKNOWN")
            log.info(f"  -> Smyths API stockStatus: {hd_status}")
            return hd_status == "INSTOCK"
        else:
            log.warning(f"  -> Smyths API blocked (HTTP {r.status_code})")
            return "blocked"
    except Exception as exc:
        log.warning(f"  -> Smyths API error: {exc}")
        return False


def is_in_stock(page, item: dict) -> bool:
    url = item["url"]
    site_key = detect_site(url)
    site_cfg = SITES.get(site_key)

    # Smyths: use their inventory API directly — browser is blocked from datacenter IPs
    if site_key == "smyths":
        sku = url.rstrip("/").split("/p/")[-1]
        log.info(f"  -> Smyths inventory API (SKU: {sku})")
        return check_smyths_api(sku)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
    except PlaywrightTimeoutError:
        log.warning(f"  -> Page load timed out")
        return False
    except Exception as exc:
        log.warning(f"  -> Page load failed: {exc}")
        return False

    # Log page title so we can verify the right page loaded in CI logs
    log.info(f"  -> Page title: {page.title()}")

    content_lower = page.content().lower()

    # Check if we're being blocked or hit a CAPTCHA
    if is_blocked(content_lower):
        log.warning(f"  -> ⚠️ BLOCKED/CAPTCHA detected!")
        return "blocked"

    if site_cfg:
        # 1. CSS selector OOS check (on fully rendered DOM)
        for sel in site_cfg.get("oos_selectors", []):
            el = page.query_selector(sel)
            if el and el.text_content().strip():
                return False

        # 2. OOS text check
        for phrase in site_cfg["oos_text"]:
            if phrase in content_lower:
                return False

        # 3. CSS selector in-stock check (most reliable — checks for actual buy button)
        for sel in site_cfg.get("in_stock_selectors", []):
            el = page.query_selector(sel)
            if el:
                return True

        # 4. In-stock text check (fallback)
        for phrase in site_cfg["in_stock_text"]:
            if phrase in content_lower:
                return True

    else:
        # Generic fallback
        oos_phrases = ["out of stock", "sold out", "unavailable", "notify me"]
        in_stock_phrases = ["add to basket", "add to trolley", "add to cart", "buy now"]
        for phrase in oos_phrases:
            if phrase in content_lower:
                return False
        for phrase in in_stock_phrases:
            if phrase in content_lower:
                return True

    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_round(config: dict, notified: set) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            locale="en-GB",
        )
        page = context.new_page()

        for item in config["items"]:
            name = item["name"]
            url = item["url"]
            site_key = detect_site(url)
            site_name = SITES.get(site_key, {}).get("name", "Unknown Site")

            log.info(f"Checking [{site_name}] {name}")

            result = is_in_stock(page, item)

            if result == "blocked":
                log.warning(f"  -> ⚠️ {site_name} is blocking us")
                if f"blocked_{site_key}" not in notified:
                    send_discord_warning(config["discord_webhook"], site_name, url)
                    notified.add(f"blocked_{site_key}")
            elif result is True:
                log.info(f"  -> ✅ IN STOCK!")
                if url not in notified:
                    send_discord(config["discord_webhook"], item, site_name)
                    notified.add(url)
                else:
                    log.info(f"  -> Already notified, skipping Discord")
            else:
                log.info(f"  -> ❌ Out of stock")
                notified.discard(url)
                notified.discard(f"blocked_{site_key}")

            # Polite delay between pages
            time.sleep(random.uniform(2, 4))

        browser.close()


def main() -> None:
    config = load_config()

    # Prefer DISCORD_WEBHOOK env var (GitHub Actions secret) over config file
    webhook = os.environ.get("DISCORD_WEBHOOK") or config.get("discord_webhook", "")
    if not webhook or webhook == "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE":
        log.error("Discord webhook not configured. Set DISCORD_WEBHOOK env var or config.json. Exiting.")
        return
    config["discord_webhook"] = webhook

    items = config["items"]
    log.info(f"Stock bot started — monitoring {len(items)} item(s)")
    for i in items:
        log.info(f"  • {i['name']}")

    notified: set = set()

    # In CI (GitHub Actions) run once and exit; locally loop continuously
    if os.environ.get("CI"):
        log.info("CI mode: running single check")
        run_round(config, notified)
    else:
        interval = config.get("check_interval", 120)
        log.info(f"Local mode: checking every {interval}s")
        while True:
            try:
                run_round(config, notified)
            except Exception as exc:
                log.error(f"Unexpected error: {exc}")
            log.info(f"Sleeping {interval}s until next check...\n")
            time.sleep(interval)


if __name__ == "__main__":
    main()
