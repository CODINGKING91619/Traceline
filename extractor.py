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

DEFAULT_SUBPAGES = [
    "", "/contact", "/contact-us", "/contacts", "/get-started", "/start",
    "/book-a-call", "/about", "/about-us", "/get-in-touch"
]
REQUEST_TIMEOUT = 10
DELAY_BETWEEN_PAGES = 0.5
RENDER_TIMEOUT_MS = 15000

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(r"(\+?\d[\d\-.\s()]{8,}\d)")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

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
    """Fetches a URL using requests with fallback to http:// if https:// fails.
    Returns (soup, raw_html)."""
    urls_to_try = [url]
    if url.startswith("https://"):
        urls_to_try.append(url.replace("https://", "http://"))

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
    of the server process."""
    global _playwright, _browser
    with _browser_lock:
        if _browser is None:
            from playwright.sync_api import sync_playwright
            _playwright = sync_playwright().start()
            _browser = _playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        return _browser


def get_soup_rendered(url):
    """Loads page in Playwright using custom context (User-Agent + Viewport)
    to bypass headless bot blocks and render JS-injected elements.
    Scrolls to the bottom to trigger lazy-loaded footers and return full HTML."""
    try:
        browser = _get_browser()
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
            device_scale_factor=1,
        )
        page = context.new_page()
        try:
            page.set_default_timeout(RENDER_TIMEOUT_MS)
            try:
                page.goto(url, wait_until="load", timeout=RENDER_TIMEOUT_MS)
            except Exception:
                pass  # grab whatever HTML rendered before timeout
            page.wait_for_timeout(1000)

            # Scroll to bottom of page to trigger lazy-loaded footers & social links
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
            except Exception:
                pass

            html = page.content()
            return BeautifulSoup(html, "html.parser"), html
        finally:
            page.close()
            context.close()
    except Exception:
        return None, ""


# ---------------- shared parsing of a page's contact info ----------------

PHONE_STRICT_REGEX = re.compile(r"(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})")


def clean_phone(raw):
    digits = re.sub(r"\D", "", raw)
    # Valid phone numbers have 10 to 12 digits (e.g. 626-426-7235 or +1 626-426-7235)
    if not (10 <= len(digits) <= 12):
        return None
    # 555-01xx (5550100 through 5550199) is the reserved placeholder/fictional range in North America
    if re.search(r"55501\d{2}", digits):
        return None
    # Filter placeholder input values like (555) 123-4567 or 1234567890
    if re.search(r"5551234567|1234567890", digits):
        return None
    # Normalize phone formatting so duplicate variations collapse into one entry
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+1 {digits[1:4]}-{digits[4:7]}-{digits[7:]}"
    return raw.strip()


def decode_cf_email(cf_hex):
    try:
        key = int(cf_hex[:2], 16)
        email_bytes = [int(cf_hex[i:i+2], 16) ^ key for i in range(2, len(cf_hex), 2)]
        decoded = "".join(chr(b) for b in email_bytes)
        if EMAIL_REGEX.fullmatch(decoded) and not decoded.lower().endswith(IMAGE_EXT):
            return decoded.lower()
    except Exception:
        pass
    return None


def _scan_soup(soup, raw_html="", base_url="", fetched_scripts=None):
    """Pulls emails/phones/instagram out of one already-fetched page."""
    emails, phones = set(), set()
    instagram = None
    if not soup and not raw_html:
        return emails, phones, instagram

    if fetched_scripts is None:
        fetched_scripts = set()

    # 1. Cloudflare Email Obfuscation Decoding (data-cfemail & /cdn-cgi/l/email-protection#...)
    if soup:
        for tag in soup.find_all(attrs={"data-cfemail": True}):
            dec = decode_cf_email(tag["data-cfemail"])
            if dec:
                emails.add(dec)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "email-protection" in href:
                cf_hex = href.split("#")[-1]
                dec = decode_cf_email(cf_hex)
                if dec:
                    emails.add(dec)

    html_source = raw_html or (str(soup) if soup else "")
    if html_source:
        for match in re.findall(r'data-cfemail=["\']([a-fA-F0-9]+)["\']', html_source):
            dec = decode_cf_email(match)
            if dec:
                emails.add(dec)
        for match in re.findall(r'/email-protection#([a-fA-F0-9]+)', html_source):
            dec = decode_cf_email(match)
            if dec:
                emails.add(dec)

    # 2. Standard spaced text extraction for clean reading of body text
    if soup:
        spaced_text = soup.get_text(" ")
        for e in EMAIL_REGEX.findall(spaced_text):
            if not e.lower().endswith(IMAGE_EXT):
                emails.add(e.lower())
        for p in PHONE_REGEX.findall(spaced_text):
            cp = clean_phone(p)
            if cp:
                phones.add(cp)

        # 3. Check all individual HTML tags for letter-split animated emails (e.g. <span>H</span><span>E</span>...)
        for el in soup.find_all(True):
            if el.name in ("script", "style", "svg", "path", "img", "meta", "link"):
                continue
            el_text = el.get_text("")
            if "@" in el_text:
                for e in EMAIL_REGEX.findall(el_text):
                    if not e.lower().endswith(IMAGE_EXT):
                        emails.add(e.lower())

    # 4. Raw HTML string & JSON-LD schema scanning
    if html_source:
        for e in EMAIL_REGEX.findall(html_source):
            el = e.lower()
            if not el.endswith(IMAGE_EXT) and not any(ign in el for ign in ["w3.org", "schema.org", "example.com", "yourcompany.com"]):
                emails.add(el)

    # 4. External JS Bundle Scanning (catches phone/email stored in JS assets like index-DPpQS5OG.js)
    if base_url:
        base_netloc = urlparse(base_url).netloc.lower()
        for script in soup.find_all("script", src=True):
            src = urljoin(base_url, script["src"])
            if src in fetched_scripts:
                continue
            if urlparse(src).netloc.lower() == base_netloc:
                fetched_scripts.add(src)
                try:
                    resp = requests.get(src, headers=HEADERS, timeout=5)
                    if resp.status_code == 200:
                        text_js = resp.text
                        for e in EMAIL_REGEX.findall(text_js):
                            el = e.lower()
                            if not el.endswith(IMAGE_EXT) and not any(x in el for x in ["w3.org", "schema.org", "example.com", "yourcompany.com"]):
                                emails.add(el)
                        for match in PHONE_STRICT_REGEX.finditer(text_js):
                            start = match.start()
                            if start > 0 and text_js[start - 1].isalnum():
                                continue
                            g1, g2, g3 = match.groups()
                            if g2 == "555" and (g3.startswith("01") or g3 == "1234"):
                                continue
                            phones.add(f"+1 {g1}-{g2}-{g3}")
                except Exception:
                    pass

    # 5. Mailto, Tel, Instagram links
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


def find_contact_links(soup, base_url):
    """Discovers candidate contact page URLs from <a> tags on the page."""
    links = []
    if not soup:
        return links
    parsed_base = urlparse(base_url)
    base_netloc = parsed_base.netloc.lower()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ").lower()
        full_url = urljoin(base_url, href)
        parsed_url = urlparse(full_url)

        # Only stay on same domain
        if parsed_url.netloc.lower() != base_netloc:
            continue

        path_lower = parsed_url.path.lower()
        if any(kw in path_lower or kw in text for kw in ["contact", "about", "reach", "touch", "connect", "start", "book"]):
            if full_url not in links and parsed_url.path not in ["", "/"]:
                links.append(full_url)
    return links[:5]  # limit to top 5 discovered links


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
        url = urljoin(base, path) if isinstance(path, str) and path.startswith("/") or path == "" else path
        if url in checked_urls:
            continue
        checked_urls.add(url)

        soup, raw_html = get_soup(url)

        # On homepage, discover any specific contact/about subpage links dynamically
        if idx == 0 and soup:
            discovered = find_contact_links(soup, base)
            for link in discovered:
                if link not in checked_urls and link not in subpages:
                    subpages.append(link)

        e1, p1, ig1 = _scan_soup(soup, raw_html, base, fetched_scripts)
        emails |= e1
        phones |= p1
        if not instagram and ig1:
            instagram = ig1

        # If any contact detail is missing, try headless Playwright browser to render JS & lazy footers
        if not (e1 and p1 and ig1):
            soup_r, raw_html_r = get_soup_rendered(url)
            e2, p2, ig2 = _scan_soup(soup_r, raw_html_r, base, fetched_scripts)
            emails |= e2
            phones |= p2
            if not instagram and ig2:
                instagram = ig2

        # Early exit if all 3 fields are found
        if emails and phones and instagram:
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    return list(emails), list(phones), instagram



