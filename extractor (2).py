"""
Extraction engine: given a company website, finds email, phone, and Instagram.
Kept separate from app.py so it can be tested or reused independently.

Hybrid strategy: most sites are checked with a fast plain HTTP fetch. Only if
that finds NOTHING AT ALL on a given page do we fall back to a real headless
browser (Playwright) that runs the page's JavaScript -- this catches sites
that inject their contact info client-side, at the cost of being much slower
for that subset of sites. Deliberately kept lean: fewer guessed subpages and
an early exit as soon as something is found, rather than exhaustively trying
to find all of email+phone+Instagram together, since that dramatically
increases how often the slow path fires.
"""

import queue
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SUBPAGES_TO_CHECK = ["", "/contact", "/contact-us", "/about", "/about-us"]
REQUEST_TIMEOUT = 10
DELAY_BETWEEN_PAGES = 0.5
RENDER_TIMEOUT_MS = 15000
HARD_RENDER_TIMEOUT_SEC = 22   # absolute ceiling per page render, even if Playwright's own timeout fails to fire
RECYCLE_BROWSER_EVERY = 40     # relaunch the browser periodically on long batches
COMPANY_TIME_BUDGET_SEC = 60   # hard ceiling on total time spent per company, across all its subpages

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
_render_count = 0


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


def _reset_browser():
    """Force-closes and discards the browser so the next call launches a
    fresh one -- used after a stuck render, or periodically on long
    batches, since a long-running instance can slowly become unstable
    (especially under limited hosting RAM)."""
    global _browser, _playwright
    with _browser_lock:
        try:
            if _browser:
                _browser.close()
        except Exception:
            pass
        try:
            if _playwright:
                _playwright.stop()
        except Exception:
            pass
        _browser = None
        _playwright = None


def _get_soup_rendered_raw(url):
    """Loads the page in a real (headless) browser, waits for it to load
    and gives any late JavaScript a moment to finish, then returns the
    fully-rendered HTML.

    Deliberately does NOT wait for full "network idle" -- a page with a
    live chat widget or analytics script polling in the background may
    never go idle, which would time out and lose everything. Waiting for
    the load event plus a short fixed pause is more forgiving.
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


def get_soup_rendered(url):
    """Public entry point: runs the actual render in a background thread and
    gives up after a hard ceiling no matter what Playwright itself is doing.
    Playwright's own timeouts are best-effort and can fail to fire if the
    browser process itself becomes unresponsive, so this is a second,
    unconditional deadline on top of that -- this is what stops one bad
    site from freezing an entire batch."""
    global _render_count

    result_queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result_queue.put(_get_soup_rendered_raw(url))
        except Exception:
            result_queue.put(None)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(HARD_RENDER_TIMEOUT_SEC)

    if t.is_alive():
        _reset_browser()
        return None

    with _browser_lock:
        _render_count += 1
        should_recycle = _render_count % RECYCLE_BROWSER_EVERY == 0
    if should_recycle:
        _reset_browser()

    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return None


# ---------------- phone cleaning ----------------

def clean_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return None
    if re.search(r"55501\d{2}", digits):
        return None  # reserved fictional range (555-0100 to 555-0199)
    return raw.strip()


# ---------------- Cloudflare email de-obfuscation ----------------

def decode_cf_email(cf_hex):
    """Decodes Cloudflare's 'Email Protection' obfuscation
    (data-cfemail="..." / /cdn-cgi/l/email-protection#...), which many
    WordPress/Elementor sites use to hide mailto addresses from scrapers."""
    try:
        key = int(cf_hex[:2], 16)
        email_bytes = [int(cf_hex[i:i + 2], 16) ^ key for i in range(2, len(cf_hex), 2)]
        decoded = "".join(chr(b) for b in email_bytes)
        if EMAIL_REGEX.fullmatch(decoded) and not decoded.lower().endswith(IMAGE_EXT):
            return decoded.lower()
    except Exception:
        pass
    return None


# ---------------- shared parsing of a page's contact info ----------------

def _scan_soup(soup):
    """Pulls emails/phones/instagram out of one already-fetched page."""
    emails, phones = set(), set()
    instagram = None
    if not soup:
        return emails, phones, instagram

    # Cloudflare-obfuscated emails
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        dec = decode_cf_email(tag["data-cfemail"])
        if dec:
            emails.add(dec)
    for a in soup.find_all("a", href=True):
        if "email-protection" in a["href"]:
            dec = decode_cf_email(a["href"].split("#")[-1])
            if dec:
                emails.add(dec)

    # visible text with normal spacing
    spaced_text = soup.get_text(" ")
    for e in EMAIL_REGEX.findall(spaced_text):
        if not e.lower().endswith(IMAGE_EXT):
            emails.add(e.lower())
    for p in PHONE_REGEX.findall(spaced_text):
        cp = clean_phone(p)
        if cp:
            phones.add(cp)

    # letter-split animated headings (one <span> per character): only
    # trust an element if its DIRECT children are each just 1-2 characters,
    # which is the actual signature of a per-letter animation -- this
    # avoids merging unrelated sibling elements into a fake email
    for el in soup.find_all(True):
        if el.name in ("script", "style", "svg", "path", "img", "meta", "link"):
            continue
        direct_children = [c for c in el.children if getattr(c, "name", None)]
        if len(direct_children) < 4:
            continue
        child_texts = [c.get_text("") for c in direct_children]
        if all(0 < len(t) <= 2 for t in child_texts):
            joined = "".join(child_texts)
            if "@" in joined:
                for e in EMAIL_REGEX.findall(joined):
                    if not e.lower().endswith(IMAGE_EXT):
                        emails.add(e.lower())

    # mailto: / tel: / instagram links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if EMAIL_REGEX.fullmatch(addr) and not addr.lower().endswith(IMAGE_EXT):
                emails.add(addr.lower())
        elif href.lower().startswith("tel:"):
            cp = clean_phone(href[4:].strip())
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

    if not base_url:
        return [], [], None
    base_url = base_url.strip().strip('"').strip("'")
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    company_start = time.time()

    for path in SUBPAGES_TO_CHECK:
        if time.time() - company_start > COMPANY_TIME_BUDGET_SEC:
            break

        url = urljoin(base, path)

        soup = get_soup(url)
        e1, p1, ig1 = _scan_soup(soup)
        emails |= e1
        phones |= p1
        if not instagram and ig1:
            instagram = ig1

        # only pay for the slow browser step if this page found NOTHING at
        # all via the fast fetch -- this is what keeps normal sites fast
        if not (e1 or p1 or ig1):
            soup_r = get_soup_rendered(url)
            e2, p2, ig2 = _scan_soup(soup_r)
            emails |= e2
            phones |= p2
            if not instagram and ig2:
                instagram = ig2

        time.sleep(DELAY_BETWEEN_PAGES)

    return list(emails), list(phones), instagram
