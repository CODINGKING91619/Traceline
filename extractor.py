"""
Extraction engine: given a company website, finds email, phone, and Instagram.
Kept separate from app.py so it can be tested or reused independently.

Hybrid strategy: most sites are checked with a fast plain HTTP fetch. Only if
that finds nothing on a given page do we fall back to a real headless browser
(Playwright) that runs the page's JavaScript -- this catches sites (Framer,
some React/Next builds) that inject their contact info client-side, at the
cost of being much slower for that subset of sites.
"""

import re
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SUBPAGES_TO_CHECK = ["", "/contact", "/contact-us", "/about", "/about-us"]
REQUEST_TIMEOUT = 10
DELAY_BETWEEN_PAGES = 0.6
RENDER_TIMEOUT_MS = 15000

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(r"(\+?\d[\d\-.\s()]{8,}\d)")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}

# ---------------- fast path: plain HTTP fetch ----------------

def get_soup(url):
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
            return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException:
        return None
    return None


# ---------------- slow path: headless browser (JS-rendered) ----------------

_playwright = None
_browser = None
_browser_lock = threading.Lock()


def _get_browser():
    """Launches one shared headless Chromium instance, reused for the life
    of the server process (launching a fresh browser per page would be far
    too slow)."""
    global _playwright, _browser
    with _browser_lock:
        if _browser is None:
            from playwright.sync_api import sync_playwright
            _playwright = sync_playwright().start()
            _browser = _playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
        return _browser


def get_soup_rendered(url):
    """Loads the page in a real (headless) browser, waits for it to load
    and gives any late JavaScript a moment to finish, then returns the
    fully-rendered HTML. Used only as a fallback when the fast fetch finds
    nothing, since this is much slower.

    Deliberately does NOT wait for full "network idle" — sites with a live
    chat widget or analytics script that keeps polling in the background
    would never go idle and we'd time out and lose everything. Waiting for
    the page's load event, then a short fixed pause for JS to settle, is
    more forgiving and still catches JS-injected content.
    """
    try:
        browser = _get_browser()
        page = browser.new_page()
        try:
            page.set_default_timeout(RENDER_TIMEOUT_MS)
            try:
                page.goto(url, wait_until="load", timeout=RENDER_TIMEOUT_MS)
            except Exception:
                pass  # even a slow/incomplete load may have usable content by now
            page.wait_for_timeout(1500)
            html = page.content()
            return BeautifulSoup(html, "html.parser")
        finally:
            page.close()
    except Exception:
        return None


# ---------------- shared parsing of a page's contact info ----------------

FAKE_PHONE_RE = re.compile(r"555.?01\d{2}")


def clean_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return None
    if FAKE_PHONE_RE.search(raw):
        return None  # 555-01xx is the reserved placeholder/fictional range
    return raw.strip()


def _scan_soup(soup):
    """Pulls emails/phones/instagram out of one already-fetched page."""
    emails, phones = set(), set()
    instagram = None
    if not soup:
        return emails, phones, instagram

    # visible page text, checked two ways:
    # - with spaces between elements (correct for normal flowing sentences)
    # - with no spaces (catches "letter-split" animated headings some sites
    #   use, e.g. HELLO@COMPANY.COM rendered as one <span> per character,
    #   which the space-separated version would otherwise break apart)
    for text in (soup.get_text(" "), soup.get_text("")):
        for e in EMAIL_REGEX.findall(text):
            if not e.lower().endswith(IMAGE_EXT):
                emails.add(e.lower())
        for p in PHONE_REGEX.findall(text):
            cp = clean_phone(p)
            if cp:
                phones.add(cp)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if EMAIL_REGEX.fullmatch(addr) and not addr.lower().endswith(IMAGE_EXT):
                emails.add(addr.lower())

        elif href.lower().startswith("tel:"):
            raw = href[4:].strip()
            cp = clean_phone(raw)
            if cp:
                phones.add(cp)

        elif not instagram and ("instagram.com" in href or "instagr.am" in href):
            if "/p/" not in href and "/reel/" not in href:
                instagram = href.split("?")[0]

    return emails, phones, instagram


def extract_contacts(base_url):
    """Returns (emails: list[str], phones: list[str], instagram: str|None)"""
    emails, phones = set(), set()
    instagram = None

    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    for path in SUBPAGES_TO_CHECK:
        url = urljoin(base, path)

        soup = get_soup(url)
        e1, p1, ig1 = _scan_soup(soup)
        emails |= e1
        phones |= p1
        if not instagram and ig1:
            instagram = ig1

        if not (e1 or p1 or ig1):
            soup_r = get_soup_rendered(url)
            e2, p2, ig2 = _scan_soup(soup_r)
            emails |= e2
            phones |= p2
            if not instagram and ig2:
                instagram = ig2

        time.sleep(DELAY_BETWEEN_PAGES)

    return list(emails), list(phones), instagram
