import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

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
        # Specific button selector — most reliable signal (from streetmerchant)
        "in_stock_selectors": ["#addToCartButton"],
        # Text found on page when IN stock (fallback)
        "in_stock_text": ["add to basket", "add to cart"],
        "oos_text": ["out of stock", "sold out", "notify me when available", "pre-order"],
        # instoreMessage selector from streetmerchant is more precise than generic class
        "oos_selectors": [".instoreMessage", ".out-of-stock", ".notifyMe"],
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
# Browser-like headers to avoid blocks
# ---------------------------------------------------------------------------
HEADER_SETS = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    },
]


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


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(random.choice(HEADER_SETS))
    return session


# ---------------------------------------------------------------------------
# Stock checking
# ---------------------------------------------------------------------------

def is_in_stock(session: requests.Session, item: dict) -> bool:
    url = item["url"]
    site_key = detect_site(url)
    site_cfg = SITES.get(site_key)

    try:
        resp = session.get(url, timeout=20, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning(f"  -> Request failed: {exc}")
        return False

    html = resp.text
    content_lower = html.lower()
    soup = BeautifulSoup(html, "lxml")

    if site_cfg:
        # 1. CSS selector OOS check
        for sel in site_cfg.get("oos_selectors", []):
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return False

        # 2. OOS text check
        for phrase in site_cfg["oos_text"]:
            if phrase in content_lower:
                return False

        # 3. CSS selector in-stock check (most reliable — checks for actual buy button)
        for sel in site_cfg.get("in_stock_selectors", []):
            el = soup.select_one(sel)
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
    session = make_session()

    for item in config["items"]:
        name = item["name"]
        url = item["url"]
        site_key = detect_site(url)
        site_name = SITES.get(site_key, {}).get("name", "Unknown Site")

        log.info(f"Checking [{site_name}] {name}")

        in_stock = is_in_stock(session, item)

        if in_stock:
            log.info(f"  -> ✅ IN STOCK!")
            if url not in notified:
                send_discord(config["discord_webhook"], item, site_name)
                notified.add(url)
            else:
                log.info(f"  -> Already notified, skipping Discord")
        else:
            log.info(f"  -> ❌ Out of stock")
            notified.discard(url)

        # Polite delay between requests
        time.sleep(random.uniform(3, 6))


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
