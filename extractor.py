"""
Extraction engine: given a company website, finds email, phone, and Instagram.
Kept separate from app.py so it can be tested or reused independently.
"""

import queue
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SUBPAGES_TO_CHECK = ["", "/contact", "/contact-us", "/get-in-touch", "/about", "/about-us"]
REQUEST_TIMEOUT = 6
DELAY_BETWEEN_PAGES = 0.2
RENDER_TIMEOUT_MS = 4000
HARD_RENDER_TIMEOUT_SEC = 6   # absolute ceiling per page render
RECYCLE_BROWSER_EVERY = 10     # relaunch browser frequently to keep RAM low on 512MB hosting
COMPANY_TIME_BUDGET_SEC = 25   # hard ceiling on total time spent per company, across all its subpages

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(r"(\+?\d[\d\-.\s()]{8,}\d)")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

IG_RESERVED = {
    "p", "reel", "reels", "stories", "story", "explore", "tv", "direct",
    "accounts", "account", "sharer", "share", "developer", "developers", "about", "legal",
    "privacy", "terms", "help", "blog", "press", "api", "graphql", "create",
    "directory", "channel", "embed", "static", "style", "challenge", "location", "locations",
    "tags", "oauth", "login", "signup", "auth", "advertising", "advertisers", "ads", "business",
    "testimonial", "testimonials", "contact", "careers", "jobs", "pricing", "features",
    "services", "home", "main", "index", "site", "link", "links", "bio", "profile", "profiles",
    "post", "posts", "media", "video", "videos", "image", "images", "photo", "photos",
    "download", "app", "apps", "divider", "title", "feed", "tab", "films", "film", "menu",
    "header", "footer", "sidebar", "content", "wrapper", "container", "row", "col", "column",
    "grid", "button", "btn", "nav", "navbar", "modal", "dialog", "popup",
    "square", "circle", "round", "icon", "icons", "logo", "logos", "banner", "banners", "avatar", "thumbnail", "thumb",
    "before", "after", "above", "below", "next", "prev", "previous"
}


def extract_ig_username(val):
    """Extracts valid Instagram username from a URL, handle, or string."""
    if not val:
        return None
    val = str(val).strip()
    if val.startswith("@"):
        val = val[1:].strip()

    if val.startswith("//"):
        val = "https:" + val
    elif not val.startswith(("http://", "https://")):
        if "instagram.com" in val.lower() or "instagr.am" in val.lower():
            val = "https://" + val
        else:
            if (re.fullmatch(r"[a-zA-Z0-9._]{2,30}", val) and 
                val.lower() not in IG_RESERVED and
                re.search(r"[a-zA-Z]", val) and
                not val.lower().endswith((".html", ".htm", ".php", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")) and
                not re.fullmatch(r"\d+x\d+", val.lower())):
                return val
            return None

    try:
        parsed = urlparse(val)
        netloc = parsed.netloc.lower()
        if "instagram.com" not in netloc and "instagr.am" not in netloc:
            return None

        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        if not path_parts:
            return None

        username = path_parts[0]
        if username.lower() in IG_RESERVED:
            return None

        if username.lower().endswith((".html", ".htm", ".php", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
            return None

        if (re.fullmatch(r"[a-zA-Z0-9._]{1,30}", username) and
            re.search(r"[a-zA-Z]", username) and
            not re.fullmatch(r"\d+x\d+", username.lower())):
            return username
    except Exception:
        pass
    return None

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

_session = None
_session_lock = threading.Lock()

def get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=1)
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                _session = s
    return _session

def get_soup(url):
    parsed = urlparse(url)
    netloc = parsed.netloc or parsed.path.split("/")[0]
    path = parsed.path if parsed.scheme else ""
    if parsed.query:
        path += "?" + parsed.query

    hosts = [netloc]
    if netloc.startswith("www."):
        if netloc[4:] not in hosts:
            hosts.append(netloc[4:])
    else:
        if ("www." + netloc) not in hosts:
            hosts.append("www." + netloc)

    urls_to_try = []
    for host in hosts:
        urls_to_try.append(f"https://{host}{path}")
        urls_to_try.append(f"http://{host}{path}")

    session = get_session()
    for u in urls_to_try:
        for ua in [UA_CHROME, UA_GOOGLEBOT]:
            try:
                headers = {
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
                resp = session.get(u, timeout=REQUEST_TIMEOUT, headers=headers, verify=False, allow_redirects=True)
                if resp and resp.text and len(resp.text) > 200:
                    ct = resp.headers.get("Content-Type", "").lower()
                    if any(media in ct for media in ["image/", "video/", "audio/", "application/pdf", "font/", "zip"]):
                        continue
                    if resp.status_code == 200 or "html" in ct or "<html" in resp.text[:500].lower() or "<body" in resp.text[:500].lower():
                        return BeautifulSoup(resp.text, "html.parser"), resp.text
            except Exception:
                pass
    return None, None


import os
import gc

# Enable Playwright by default so headless browser renders client-side JS footers/widgets on hosting
ENABLE_PLAYWRIGHT = os.environ.get("ENABLE_PLAYWRIGHT", "1").lower() in ("1", "true", "yes")
DISABLE_PLAYWRIGHT = not ENABLE_PLAYWRIGHT

_playwright = None
_browser = None
_browser_lock = threading.Lock()
_render_count = 0


def _get_browser():
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
                    "--js-flags=--max-old-space-size=128",
                    "--no-zygote",
                    "--disable-extensions",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-default-apps",
                    "--mute-audio",
                    "--no-first-run",
                ],
            )
        return _browser


def _reset_browser():
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
    gc.collect()


def _get_soup_rendered_raw(url):
    try:
        browser = _get_browser()
        page = browser.new_page()
        try:
            page.set_default_timeout(RENDER_TIMEOUT_MS)
            try:
                page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2,ttf,mp4,webm,ico}", lambda route: route.abort())
            except Exception:
                pass
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
            except Exception:
                pass
            page.wait_for_timeout(500)
            html = page.content()
            return BeautifulSoup(html, "html.parser"), html
        finally:
            page.close()
    except Exception:
        return None, None


def get_soup_rendered(url):
    if DISABLE_PLAYWRIGHT:
        return None, None

    global _render_count
    result_queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result_queue.put(_get_soup_rendered_raw(url))
        except Exception:
            result_queue.put((None, None))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(HARD_RENDER_TIMEOUT_SEC)

    if t.is_alive():
        _reset_browser()
        return None, None

    with _browser_lock:
        _render_count += 1
        should_recycle = _render_count % RECYCLE_BROWSER_EVERY == 0
    if should_recycle:
        _reset_browser()

    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return None, None


def clean_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return None
    if re.search(r"55501\d{2}", digits):
        return None
    return raw.strip()


def decode_cf_email(cf_hex):
    try:
        key = int(cf_hex[:2], 16)
        email_bytes = [int(cf_hex[i:i + 2], 16) ^ key for i in range(2, len(cf_hex), 2)]
        decoded = "".join(chr(b) for b in email_bytes)
        if EMAIL_REGEX.fullmatch(decoded) and not decoded.lower().endswith(IMAGE_EXT):
            return decoded.lower()
    except Exception:
        pass
    return None


def _scan_soup(soup, html_text=None):
    emails, phones = set(), set()
    instagram = None
    if not soup and not html_text:
        return emails, phones, instagram

    if soup:
        for tag in soup.find_all(attrs={"data-cfemail": True}):
            dec = decode_cf_email(tag["data-cfemail"])
            if dec:
                emails.add(dec)
        for a in soup.find_all("a", href=True):
            if "email-protection" in a["href"]:
                dec = decode_cf_email(a["href"].split("#")[-1])
                if dec:
                    emails.add(dec)

        spaced_text = soup.get_text(" ")
        for e in EMAIL_REGEX.findall(spaced_text):
            if not e.lower().endswith(IMAGE_EXT):
                emails.add(e.lower())
        for p in PHONE_REGEX.findall(spaced_text):
            cp = clean_phone(p)
            if cp:
                phones.add(cp)

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

    ig_candidates = []

    if soup:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            uname = extract_ig_username(href)
            if uname:
                is_priority = False
                parent = a.parent
                for _ in range(5):
                    if parent and getattr(parent, "name", None):
                        tag_str = (str(parent.get("class", [])) + str(parent.get("id", "")) + parent.name).lower()
                        if any(k in tag_str for k in ["footer", "social", "header", "follow", "nav"]):
                            is_priority = True
                            break
                        parent = parent.parent
                    else:
                        break
                score = 25 if is_priority else 7
                ig_candidates.append((score, uname, f"https://www.instagram.com/{uname}/"))

        for tag in soup.find_all(True):
            for attr in ["data-href", "data-url", "data-src", "data-link", "src", "onclick"]:
                val = tag.get(attr)
                if val and ("instagram.com" in str(val).lower() or "instagr.am" in str(val).lower()):
                    uname = extract_ig_username(str(val))
                    if uname:
                        ig_candidates.append((5, uname, f"https://www.instagram.com/{uname}/"))

        for tag in soup.find_all(["script", "meta", "link"]):
            content = tag.get_text() or tag.get("content") or tag.get("href") or ""
            if "instagram.com" in content.lower() or "instagr.am" in content.lower():
                matches = re.finditer(r"(?:https?:)?//(?:www\.)?(?:instagram\.com|instagr\.am)/([a-zA-Z0-9._]{1,30})", content, re.IGNORECASE)
                for m in matches:
                    uname = extract_ig_username(m.group(1))
                    if uname:
                        ig_candidates.append((6, uname, f"https://www.instagram.com/{uname}/"))

    if html_text:
        matches = re.finditer(r"(?:https?:)?//(?:www\.)?(?:instagram\.com|instagr\.am)/([a-zA-Z0-9._]{1,30})", html_text, re.IGNORECASE)
        for m in matches:
            uname = extract_ig_username(m.group(1))
            if uname:
                ig_candidates.append((4, uname, f"https://www.instagram.com/{uname}/"))

        text_matches = re.finditer(r"(?:instagram|ig|insta)\s*[:@-]\s*@?([a-zA-Z0-9._]{2,30})", html_text, re.IGNORECASE)
        for m in text_matches:
            uname = extract_ig_username(m.group(1))
            if uname and not uname.lower().endswith((".com", ".net", ".org", ".png", ".jpg", ".js", ".css")):
                ig_candidates.append((3, uname, f"https://www.instagram.com/{uname}/"))

    if ig_candidates:
        ig_candidates.sort(key=lambda x: x[0], reverse=True)
        instagram = ig_candidates[0][2]

    return emails, phones, instagram


def extract_contacts(base_url):
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
    rendered_once = False

    for path in SUBPAGES_TO_CHECK:
        if time.time() - company_start > COMPANY_TIME_BUDGET_SEC:
            break

        url = urljoin(base, path)

        soup, html_text = get_soup(url)
        e1, p1, ig1 = _scan_soup(soup, html_text)
        emails |= e1
        phones |= p1
        if not instagram and ig1:
            instagram = ig1

        # Fallback to headless browser render if Instagram is missing and Playwright is enabled
        should_render = (not instagram) and (not rendered_once) and (not DISABLE_PLAYWRIGHT)
        if should_render:
            rendered_once = True
            soup_r, html_r = get_soup_rendered(url)
            if soup_r or html_r:
                e2, p2, ig2 = _scan_soup(soup_r, html_r)
                emails |= e2
                phones |= p2
                if not instagram and ig2:
                    instagram = ig2

        # Only break early if ALL THREE (email, phone, AND instagram) are found!
        if emails and phones and instagram:
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    return list(emails), list(phones), instagram
