"""Check property sites for listings not seen before and push them to ntfy.

Sources:
  relaam - a pre-filtered search URL; every property link on it is a listing.
  mpm    - MPM Properties; the filter form is filled in (Abu Dhabi, Apartment,
           Rent, 1 bed) and every results page is collected.

Seen listing URLs are stored per source in seen.json (committed by the workflow).
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, sync_playwright

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "seen.json"
DEBUG_DIR = ROOT / "debug"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


# ---------------------------------------------------------------- helpers

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


def scroll_to_bottom(page: Page, rounds: int = 15) -> None:
    last = 0
    for _ in range(rounds):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1500)
        height = page.evaluate("document.body.scrollHeight")
        if height == last:
            break
        last = height


def collect_links(page: Page, listing_re: str) -> list[dict]:
    """Every listing link with the text of its card: the largest ancestor that
    contains no link to a *different* listing."""
    return page.eval_on_selector_all(
        "a[href]",
        """(els, pattern) => {
            const re = new RegExp(pattern, 'i');
            const key = h => { const m = h.match(re); return m ? m[0] : null; };
            return els.filter(a => key(a.href)).map(a => {
                const k = key(a.href);
                let card = a;
                while (card.parentElement && card.parentElement !== document.body &&
                       (card.parentElement.innerText || '').length < 800 &&
                       ![...card.parentElement.querySelectorAll('a[href]')]
                           .some(o => { const ok = key(o.href); return ok && ok !== k; })) {
                    card = card.parentElement;
                }
                return {href: a.href, text: (card.innerText || '').trim()};
            });
        }""",
        listing_re,
    )


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:300]


def open_page(page: Page, url: str, ready_selector: str) -> None:
    """Load a page without waiting for full network idle (analytics can keep it busy)."""
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    try:
        page.wait_for_selector(ready_selector, timeout=45_000)
    except Exception:
        print(f"  (no '{ready_selector}' appeared on {url})")
    page.wait_for_timeout(2000)


def link_count(page: Page, needle: str) -> int:
    return page.evaluate(
        "n => [...document.querySelectorAll('a[href]')].filter(a => a.href.includes(n)).length",
        needle,
    )


def snapshot(page: Page, name: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)


# ---------------------------------------------------------------- relaam

RELAAM_URL = "https://search.relaam.com/en/properties/1-bedroom-apartments-for-rent-in-abu-dhabi"
RELAAM_PATH = re.compile(r"/(propert|listing|unit|apartment)", re.I)
RELAAM_ID = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
RELAAM_KEY = r"/(propert|listing|unit|apartment)[^?#]*(\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-)"


def scrape_relaam(page: Page) -> dict[str, str]:
    open_page(page, RELAAM_URL, "a[href*='/properties/apartment']")
    scroll_to_bottom(page)
    search_path = urlparse(RELAAM_URL).path.rstrip("/")
    listings: dict[str, str] = {}
    for link in collect_links(page, RELAAM_KEY):
        url = urljoin(RELAAM_URL, link["href"]).split("#")[0]
        path = urlparse(url).path.rstrip("/")
        if path == search_path or not RELAAM_PATH.search(path) or not RELAAM_ID.search(path):
            continue
        text = clean(link["text"])
        if len(text) > len(listings.get(url, "")):
            listings[url] = text
    snapshot(page, "relaam")
    return listings


# ---------------------------------------------------------------- mpm

MPM_URL = "https://mpmproperties.ae/en/properties/residential-properties"
MPM_FILTERS = {  # dropdown -> option to pick; each dropdown is found by its options
    "city": ("Abu Dhabi", ["Abu Dhabi", "Dubai", "Sharjah"]),
    "unit type": ("Apartment", ["Apartment", "Villa"]),
    "category": ("Rent", ["Rent", "Sale"]),
    "beds": ("1", ["1", "2", "3", "4", "5"]),
}
MPM_LISTING = re.compile(r"PropertyId=([A-Za-z0-9-]+)", re.I)
# Safety net in case a filter silently didn't apply: drop cards whose title
# clearly says studio or a bedroom count other than 1.
OTHER_BEDS = re.compile(r"\bstudio\b|\b[2-9]\s*(br|b/r|bhk|bed|bedroom)s?\b", re.I)

SET_SELECT_JS = """([want, needs]) => {
    for (const sel of document.querySelectorAll('select')) {
        const labels = [...sel.options].map(o => o.text.trim());
        if (!needs.every(n => labels.includes(n))) continue;
        const opt = [...sel.options].find(o => o.text.trim() === want);
        sel.value = opt.value;
        sel.dispatchEvent(new Event('input', {bubbles: true}));
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        // Refresh common custom-dropdown widgets layered over native selects.
        if (window.jQuery) {
            const $s = window.jQuery(sel);
            $s.trigger('change');
            if ($s.selectpicker) { try { $s.selectpicker('refresh'); } catch (e) {} }
            if ($s.niceSelect) { try { $s.niceSelect('update'); } catch (e) {} }
        }
        return true;
    }
    return false;
}"""


CLICK_SEARCH_JS = """() => {
    const isBtn = el => /^(button|input|a)$/i.test(el.tagName) &&
        (el.type === 'submit' || /search|find|filter|apply/i.test(el.innerText || el.value || ''));
    const city = [...document.querySelectorAll('select')]
        .find(s => [...s.options].some(o => o.text.trim() === 'Abu Dhabi'));
    for (let node = city; node; node = node.parentElement) {
        const btn = [...node.querySelectorAll('button, input[type=submit], input[type=button], a')]
            .find(isBtn);
        if (btn) { btn.click(); return (btn.tagName + ' ' + (btn.innerText || btn.value || '')).trim().slice(0, 60); }
    }
    return false;
}"""

# Click the pagination's "Next" (an element whose container also has "Last").
CLICK_NEXT_JS = """() => {
    const cands = [...document.querySelectorAll('a, button, li, span')]
        .filter(el => /^\\s*(next|›|»|>)\\s*$/i.test(el.innerText || ''));
    for (const el of cands) {
        const bar = el.closest('ul, nav, div');
        if (!bar || !/last/i.test(bar.innerText)) continue;
        const target = el.closest('a, button') || el.querySelector('a, button') || el;
        const state = (target.className + ' ' + (target.parentElement?.className || '') + ' ' +
                       (target.getAttribute('aria-disabled') || ''));
        if (/disabled|true/i.test(state)) return false;
        target.click();
        return true;
    }
    return false;
}"""


def mpm_page_listings(page: Page) -> dict[str, str]:
    found: dict[str, str] = {}
    for link in collect_links(page, MPM_LISTING.pattern):
        m = MPM_LISTING.search(link["href"])
        if not m:
            continue
        url = f"https://mpmproperties.ae/en/properties/property-details?PropertyId={m.group(1)}"
        text = clean(link["text"])
        if len(text) > len(found.get(url, "")):
            found[url] = text
    return found


def scrape_mpm(page: Page) -> dict[str, str]:
    open_page(page, MPM_URL, "a[href*='PropertyId=']")
    print(f"MPM: {link_count(page, 'PropertyId=')} listing links before filtering")

    missing = [
        name for name, (want, needs) in MPM_FILTERS.items()
        if not page.evaluate(SET_SELECT_JS, [want, needs])
    ]
    if missing:
        raise RuntimeError(f"MPM filter dropdowns not found: {', '.join(missing)}")

    # Submit the filter form: the nearest search/submit button around the dropdowns.
    clicked = page.evaluate(CLICK_SEARCH_JS)
    print(f"MPM: search button: {clicked or 'not found (relying on auto-filter)'}")
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    page.wait_for_timeout(4000)
    selected = page.evaluate(
        "() => [...document.querySelectorAll('select')].map(s => s.options[s.selectedIndex]?.text.trim())"
    )
    print(f"MPM: after search url={page.url} selects={selected} "
          f"listing links={link_count(page, 'PropertyId=')}")

    listings: dict[str, str] = {}
    for page_no in range(1, 31):
        before = len(listings)
        listings.update(mpm_page_listings(page))
        if page_no == 1:
            snapshot(page, "mpm")
        if len(listings) == before or not page.evaluate(CLICK_NEXT_JS):
            break
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(2500)

    return {
        url: text for url, text in listings.items()
        if "abu dhabi" in text.lower() and not OTHER_BEDS.search(text)
    }


# ---------------------------------------------------------------- main

SOURCES = {
    "relaam": ("Relaam", RELAAM_URL, scrape_relaam),
    "mpm": ("MPM", MPM_URL, scrape_mpm),
}


def load_state() -> dict:
    if not SEEN_FILE.exists():
        return {"sources": {}}
    data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    if "sources" not in data:  # old single-site format -> Relaam
        data = {"sources": {"relaam": {"listings": data.get("listings", {})}}}
    return data


def check(key: str, scrape, page: Page, state: dict) -> None:
    label, url, _ = SOURCES[key]
    src = state["sources"].setdefault(key, {})
    seen: dict = src.setdefault("listings", {})
    first_run = not seen

    try:
        current = scrape(page)
    except Exception as exc:  # one broken site shouldn't stop the others
        print(f"{label}: error: {exc}")
        current, error = {}, str(exc)[:200]
    else:
        error = ""
    print(f"{label}: found {len(current)} listings")

    if not current:
        if not src.get("empty_warned"):
            notify(
                f"{label} watcher: no listings found",
                (error or "No listing links were detected.")
                + " The site may have changed; check the page snapshot in the workflow run.",
                url,
                tags="warning",
            )
            src["empty_warned"] = True
        return
    src.pop("empty_warned", None)

    new = {u: t for u, t in current.items() if u not in seen}
    if first_run:
        notify(
            f"{label} watcher is running",
            f"Tracking {len(current)} current listings. You'll be notified about new ones.",
            url,
            tags="white_check_mark",
        )
    elif len(new) > 5:
        notify(
            f"{len(new)} new {label} listings",
            "\n\n".join(t[:120] for t in list(new.values())[:8]),
            url,
        )
    else:
        for link, text in new.items():
            notify(f"New {label} listing", text or link, link)

    for link, text in current.items():
        seen.setdefault(link, text)
    print(f"{label}: {len(new)} new")


def main() -> int:
    only = sys.argv[1:]  # optional: run specific sources, e.g. `python watch.py mpm`
    state = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for key, (_, _, scrape) in SOURCES.items():
            if only and key not in only:
                continue
            page = browser.new_page(viewport={"width": 1366, "height": 900}, user_agent=USER_AGENT)
            check(key, scrape, page, state)
            page.close()
        browser.close()
    SEEN_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
