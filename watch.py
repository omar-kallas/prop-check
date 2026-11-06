"""Check a Relaam search page for listings not seen before and push them to ntfy.

Selector-free: renders the page in headless Chromium, scrolls to trigger lazy
loading, and treats every link that points to an individual property as a
listing. Seen listing URLs are stored in seen.json (committed back by the workflow).
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

SEARCH_URL = os.environ.get(
    "SEARCH_URL",
    "https://search.relaam.com/en/properties/1-bedroom-apartments-for-rent-in-abu-dhabi",
)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
SEEN_FILE = Path(__file__).with_name("seen.json")
DEBUG_DIR = Path(__file__).with_name("debug")

# A link counts as a listing if its path looks like a single property page:
# mentions property/listing/unit/apartment AND carries an ID (4+ digits or a UUID),
# so category links such as ".../2-bedroom-apartments-for-rent..." are ignored.
LISTING_PATH = re.compile(r"/(propert|listing|unit|apartment)", re.I)
LISTING_ID = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def notify(title: str, body: str, click: str | None = None, tags: str = "house") -> None:
    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC] {title}: {body}")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        method="POST",
        headers={"Title": title.encode("utf-8"), "Tags": tags},
    )
    if click:
        req.add_header("Click", click)
    urllib.request.urlopen(req, timeout=30).read()


def scrape() -> dict[str, str]:
    """Return {listing_url: short description}."""
    listings: dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
            ),
        )
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=90_000)
        # Scroll to the bottom a few times so lazy-loaded cards render.
        last_height = 0
        for _ in range(15):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1500)
            height = page.evaluate("document.body.scrollHeight")
            if height == last_height:
                break
            last_height = height

        links = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(a => {
                // Use the nearest card-like ancestor's text as a description.
                let card = a;
                for (let i = 0; i < 4 && card.parentElement; i++) {
                    card = card.parentElement;
                    if ((card.innerText || '').length > 60) break;
                }
                return {href: a.href, text: (card.innerText || a.innerText || '').trim()};
            })""",
        )
        search_path = urlparse(SEARCH_URL).path.rstrip("/")
        for link in links:
            url = urljoin(SEARCH_URL, link["href"]).split("#")[0]
            path = urlparse(url).path.rstrip("/")
            if (
                path == search_path
                or not LISTING_PATH.search(path)
                or not LISTING_ID.search(path)
            ):
                continue
            text = re.sub(r"\s+", " ", link["text"])[:300]
            if len(text) > len(listings.get(url, "")):
                listings[url] = text

        # Keep a snapshot so the first runs can be checked if nothing is found.
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
        browser.close()
    return listings


def main() -> int:
    seen_data = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    seen: dict = seen_data.get("listings", {})
    first_run = not seen_data

    current = scrape()
    print(f"Found {len(current)} listings on the page")

    if not current:
        # Don't touch seen.json; warn at most once until listings reappear.
        if not seen_data.get("empty_warned"):
            notify(
                "Relaam watcher: no listings found",
                "The page loaded but no listing links were detected. "
                "The site may have changed; check the debug snapshot in the workflow run.",
                SEARCH_URL,
                tags="warning",
            )
            seen_data["empty_warned"] = True
            SEEN_FILE.write_text(json.dumps(seen_data, indent=2, ensure_ascii=False))
        return 0

    new = {u: t for u, t in current.items() if u not in seen}

    if first_run:
        notify(
            "Relaam watcher is running",
            f"Tracking {len(current)} current listings. You'll be notified about new ones.",
            SEARCH_URL,
            tags="white_check_mark",
        )
    elif len(new) > 5:
        notify(
            f"{len(new)} new Relaam listings",
            "\n\n".join(t[:120] for t in list(new.values())[:8]),
            SEARCH_URL,
        )
    else:
        for url, text in new.items():
            notify("New Relaam listing", text or url, url)

    for url, text in current.items():
        seen.setdefault(url, text)
    SEEN_FILE.write_text(
        json.dumps({"listings": seen}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"{len(new)} new listing(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
