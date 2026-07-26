"""
Extraction engine: given a company website, finds email, phone, and Instagram.
Kept separate from app.py so it can be tested or reused independently.

Staged strategy per page, cheapest first:
  1. Fast plain HTTP fetch -- visible text, mailto/tel/instagram links,
     Cloudflare-obfuscated email decoding, letter-split animated text.
     Covers the large majority of sites almost instantly.
  2. If that page found nothing at all: scan the site's own JS bundle files
     for embedded contact info (still just HTTP requests, no browser).
  3. If STILL nothing: fall back to a real headless browser (Playwright) to
     run the page's JavaScript. This is the expensive step and is only used
     when the cheaper steps genuinely found nothing on that page.
"""

import queue
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_SUBPAGES = [
    "", "/contact", "/contact-us", "/contacts", "/about", "/about-us",
    "/get-started", "/get-in-touch",
]
REQUEST_TIMEOUT = 10
DELAY_BETWEEN_PAGES = 0.5
RENDER_TIMEOUT_MS = 15000
HARD_RENDER_TIMEOUT_SEC = 22  # absolute ceiling per page, even if Playwright's own timeout fails to fire
RECYCLE_BROWSER_EVERY = 40    # relaunch the browser periodically so a long batch can't slowly degrade it
MAX_DISCOVERED_LINKS = 3

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(r"(\+?\d[\d\-.\s()]{8,}\d)")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
IGNORED_EMAIL_DOMAINS = ("w3.org", "schema.org", "example.com", "yourcompany.com", "sentry.io", "wixpress.com")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------- fast path: plain HTTP fetch ----------------

def get_soup(url):
    """Fetches a URL, falling back from https to http if needed.
    Returns (soup, raw_html) or (None, "") on failure."""
    urls_to_try = [url]
    if url.startswith("https://"):
        urls_to_try.append(url.replace("https://", "http://", 1))

    for u in urls_to_try:
        try:
            resp = requests.get(u, timeout=REQUEST_TIMEOUT, headers=HEADERS, allow_redirects=True)
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "").lower()
                if not ct or "text/html" in ct or "xhtml" in ct:
                    return BeautifulSoup(resp.text, "html.parser"), resp.text
        except requests.RequestException:
            continue
    return None, ""


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


_render_count = 0


def _reset_browser():
    """Force-closes and discards the current browser so the next call
    launches a fresh one. Used when a render appears stuck or after
    processing a batch of pages, since a long-running Chromium instance can
    slowly leak memory / become unstable, especially under Render's limited
    RAM."""
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
    """Loads the page in a real headless browser, waits for it to load plus
    a short pause for late JS, then returns the rendered HTML.

    Deliberately does NOT wait for full "network idle" -- a page with a live
    chat widget or analytics polling in the background may never go idle,
    which would time out and lose everything. Waiting for the load event
    plus a fixed pause is more forgiving.
    """
    try:
        browser = _get_browser()
        context = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1280, "height": 900})
        page = context.new_page()
        try:
            page.set_default_timeout(RENDER_TIMEOUT_MS)
            try:
                page.goto(url, wait_until="load", timeout=RENDER_TIMEOUT_MS)
            except Exception:
                pass  # even an incomplete load may have usable content by now
            page.wait_for_timeout(1200)
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(800)
            except Exception:
                pass
            html = page.content()
            return BeautifulSoup(html, "html.parser"), html
        finally:
            page.close()
            context.close()
    except Exception:
        return None, ""


def get_soup_rendered(url):
    """Public entry point: runs the actual render in a background thread and
    gives up after a hard ceiling, no matter what Playwright itself is doing.
    This is the fix for a single stuck page freezing an entire batch --
    Playwright's own timeouts are best-effort and can fail to fire if the
    browser process itself becomes unresponsive (most often from memory
    pressure on a long run), so this is a second, unconditional deadline on
    top of that.
    """
    global _render_count

    result_queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result_queue.put(_get_soup_rendered_raw(url))
        except Exception:
            result_queue.put((None, ""))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(HARD_RENDER_TIMEOUT_SEC)

    if t.is_alive():
        # the render is stuck -- abandon it (the thread is daemon, so it
        # won't block the app from continuing) and recycle the browser,
        # since a hang like this usually means it's no longer healthy
        _reset_browser()
        return None, ""

    with _browser_lock:
        _render_count += 1
        should_recycle = _render_count % RECYCLE_BROWSER_EVERY == 0
    if should_recycle:
        _reset_browser()

    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return None, ""


# ---------------- phone cleaning ----------------

def clean_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if not (10 <= len(digits) <= 12):
        return None
    if re.search(r"55501\d{2}", digits):
        return None  # reserved fictional range
    if re.search(r"5551234567|1234567890", digits):
        return None  # obvious placeholder values
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 {digits[1:4]}-{digits[4:7]}-{digits[7:]}"
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

def _scan_soup(soup, raw_html=""):
    """Pulls emails/phones/instagram out of one already-fetched page using
    only cheap, already-downloaded data (no extra network requests)."""
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

    # visible text with normal spacing -- safe to scan as one block since
    # spaces between elements prevent unrelated text from merging together
    spaced_text = soup.get_text(" ")
    for e in EMAIL_REGEX.findall(spaced_text):
        if not e.lower().endswith(IMAGE_EXT):
            emails.add(e.lower())
    for p in PHONE_REGEX.findall(spaced_text):
        cp = clean_phone(p)
        if cp:
            phones.add(cp)

    # letter-split animated headings (one <span> per character) need a
    # no-space join to read as one email -- but joining the WHOLE page with
    # no spaces risks merging two unrelated elements into a fake address
    # (e.g. "...company.COM" + "Fake: 213..." -> "company.comfake"). Scanning
    # each element's own text separately avoids that, since it only sees
    # that element's own descendants, never its siblings.
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


def _scan_js_bundles(soup, base_url, fetched_scripts):
    """Cheaper alternative to launching a browser: downloads the site's own
    same-domain JS files as plain text and regex-scans them. Catches contact
    info that JS frameworks embed in their bundle rather than the HTML."""
    emails, phones = set(), set()
    if not soup or not base_url:
        return emails, phones

    base_netloc = urlparse(base_url).netloc.lower()
    for script in soup.find_all("script", src=True):
        src = urljoin(base_url, script["src"])
        if src in fetched_scripts or urlparse(src).netloc.lower() != base_netloc:
            continue
        fetched_scripts.add(src)
        try:
            resp = requests.get(src, headers=HEADERS, timeout=5)
            if resp.status_code != 200:
                continue
            text_js = resp.text
            for e in EMAIL_REGEX.findall(text_js):
                el = e.lower()
                if not el.endswith(IMAGE_EXT) and not any(d in el for d in IGNORED_EMAIL_DOMAINS):
                    emails.add(el)
            for p in PHONE_REGEX.findall(text_js):
                cp = clean_phone(p)
                if cp:
                    phones.add(cp)
        except requests.RequestException:
            continue

    return emails, phones


def find_contact_links(soup, base_url):
    """Finds a few likely contact/about page links from the homepage's own
    navigation, as a supplement to the fixed guess-list of common paths."""
    links = []
    if not soup:
        return links
    base_netloc = urlparse(base_url).netloc.lower()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ").lower()
        full_url = urljoin(base_url, href)
        parsed_url = urlparse(full_url)
        if parsed_url.netloc.lower() != base_netloc:
            continue
        path_lower = parsed_url.path.lower()
        if any(kw in path_lower or kw in text for kw in ("contact", "about", "reach", "touch", "connect")):
            if full_url not in links and parsed_url.path not in ("", "/"):
                links.append(full_url)
    return links[:MAX_DISCOVERED_LINKS]


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

    subpages = list(DEFAULT_SUBPAGES)
    checked_urls = set()
    fetched_scripts = set()

    for idx, path in enumerate(subpages):
        url = urljoin(base, path) if (path == "" or path.startswith("/")) else path
        if url in checked_urls:
            continue
        checked_urls.add(url)

        # -------- stage 1: fast fetch --------
        soup, raw_html = get_soup(url)

        if idx == 0 and soup:
            for link in find_contact_links(soup, base):
                if link not in checked_urls and link not in subpages:
                    subpages.append(link)

        e1, p1, ig1 = _scan_soup(soup, raw_html)
        emails |= e1
        phones |= p1
        if not instagram and ig1:
            instagram = ig1

        found_anything_on_page = bool(e1 or p1 or ig1)

        # -------- stage 2: JS bundle scan (still no browser) --------
        if not found_anything_on_page and soup:
            e2, p2 = _scan_js_bundles(soup, base, fetched_scripts)
            emails |= e2
            phones |= p2
            found_anything_on_page = bool(e2 or p2)

        # -------- stage 3: headless browser (only if truly nothing found) --------
        if not found_anything_on_page:
            soup_r, raw_html_r = get_soup_rendered(url)
            e3, p3, ig3 = _scan_soup(soup_r, raw_html_r)
            emails |= e3
            phones |= p3
            if not instagram and ig3:
                instagram = ig3

        if emails and phones and instagram:
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    return list(emails), list(phones), instagram
