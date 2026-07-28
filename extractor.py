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

SUBPAGES_TO_CHECK = ["", "/contact"]  # homepage (footer) + one contact-page guess. Kept deliberately short -- each extra page is another possible browser render, which is what was pushing memory usage too high on Render's free tier.
REQUEST_TIMEOUT = 10
DELAY_BETWEEN_PAGES = 0.5
RENDER_TIMEOUT_MS = 10000
HARD_RENDER_TIMEOUT_SEC = 16   # absolute ceiling per page render, even if Playwright's own timeout fails to fire
RECYCLE_BROWSER_EVERY = 12    # relaunch the browser periodically on long batches -- more aggressive given constrained hosting memory
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

IG_RESERVED = {
    "p", "reel", "reels", "stories", "story", "explore", "tv", "direct",
    "accounts", "account", "sharer", "share", "developer", "about", "legal",
    "privacy", "terms", "help", "blog", "press", "api", "graphql", "create",
    "directory", "channel", "embed", "static", "style", "challenge"
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
            # Maybe it's a standalone username
            if re.fullmatch(r"[a-zA-Z0-9._]{1,30}", val) and val.lower() not in IG_RESERVED:
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

        if re.fullmatch(r"[a-zA-Z0-9._]{1,30}", username):
            return username
    except Exception:
        pass
    return None

# ---------------- fast path: plain HTTP fetch ----------------

def get_soup(url):
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
            return BeautifulSoup(resp.text, "html.parser"), resp.text
    except requests.RequestException:
        return None, None
    return None, None


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
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--disable-translate",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--no-first-run",
                    "--single-process",
                    "--js-flags=--max-old-space-size=128",
                ],
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

    Also deliberately blocks images/video/fonts/stylesheets from loading --
    we only need the page's text and links, not what it visually looks
    like, and those asset types are usually what makes a real browser's
    memory usage balloon on image/video-heavy marketing sites. This is the
    main lever for staying under a constrained hosting memory limit.
    """
    try:
        browser = _get_browser()
        page = browser.new_page()
        try:
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "media", "font", "stylesheet")
                else route.continue_(),
            )
            page.set_default_timeout(RENDER_TIMEOUT_MS)
            try:
                page.goto(url, wait_until="load", timeout=RENDER_TIMEOUT_MS)
            except Exception:
                pass  # even a slow/incomplete load may have usable content by now
            page.wait_for_timeout(1000)
            html = page.content()
            return BeautifulSoup(html, "html.parser"), html
        finally:
            page.close()
    except Exception:
        return None, None


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

def _scan_soup(soup, html_text=None):
    """Pulls emails/phones/instagram out of one already-fetched page."""
    emails, phones = set(), set()
    instagram = None
    if not soup and not html_text:
        return emails, phones, instagram

    if soup:
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

        # letter-split animated headings
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

        # mailto: / tel:
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

    # ---------------- Instagram Extraction ----------------
    ig_candidates = []  # list of (score, username, url)

    if soup:
        # 0. Dedicated footer-first pass -- the Instagram link is almost
        # always in the site's <footer>, so check there specifically first
        # and score it above everything else, rather than relying only on
        # the general ancestor-keyword check below to catch it correctly.
        for footer_tag in soup.find_all("footer"):
            for a in footer_tag.find_all("a", href=True):
                uname = extract_ig_username(a["href"].strip())
                if uname:
                    ig_candidates.append((20, uname, f"https://www.instagram.com/{uname}/"))

        # some sites use a plain <div class="footer"> instead of the
        # semantic <footer> tag -- catch those too
        for footer_like in soup.find_all(
            lambda tag: tag.name in ("div", "section")
            and any("footer" in c.lower() for c in tag.get("class", []) + [tag.get("id", "")])
        ):
            for a in footer_like.find_all("a", href=True):
                uname = extract_ig_username(a["href"].strip())
                if uname:
                    ig_candidates.append((20, uname, f"https://www.instagram.com/{uname}/"))

        # 1. Check all <a> tags with href
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            uname = extract_ig_username(href)
            if uname:
                is_priority = False
                parent = a.parent
                for _ in range(8):
                    if parent and getattr(parent, "name", None):
                        tag_str = (str(parent.get("class", [])) + str(parent.get("id", "")) + parent.name).lower()
                        if any(k in tag_str for k in ["footer", "social", "header", "follow", "nav"]):
                            is_priority = True
                            break
                        parent = parent.parent
                    else:
                        break
                score = 10 if is_priority else 7
                ig_candidates.append((score, uname, f"https://www.instagram.com/{uname}/"))

        # 2. Check other elements with data attributes, src, or onclick
        for tag in soup.find_all(True):
            for attr in ["data-href", "data-url", "data-src", "data-link", "src", "onclick"]:
                val = tag.get(attr)
                if val and ("instagram.com" in str(val).lower() or "instagr.am" in str(val).lower()):
                    uname = extract_ig_username(str(val))
                    if uname:
                        ig_candidates.append((5, uname, f"https://www.instagram.com/{uname}/"))

        # 3. Check JSON-LD, meta, link tags
        for tag in soup.find_all(["script", "meta", "link"]):
            content = tag.get_text() or tag.get("content") or tag.get("href") or ""
            if "instagram.com" in content.lower() or "instagr.am" in content.lower():
                matches = re.finditer(r"(?:https?:)?//(?:www\.)?(?:instagram\.com|instagr\.am)/([a-zA-Z0-9._]{1,30})", content, re.IGNORECASE)
                for m in matches:
                    uname = m.group(1)
                    if uname.lower() not in IG_RESERVED:
                        ig_candidates.append((6, uname, f"https://www.instagram.com/{uname}/"))

    if html_text:
        # 4. Raw regex in HTML source
        matches = re.finditer(r"(?:https?:)?//(?:www\.)?(?:instagram\.com|instagr\.am)/([a-zA-Z0-9._]{1,30})", html_text, re.IGNORECASE)
        for m in matches:
            uname = m.group(1)
            if uname.lower() not in IG_RESERVED:
                ig_candidates.append((4, uname, f"https://www.instagram.com/{uname}/"))

        # 5. Mention patterns like Instagram: @username
        text_matches = re.finditer(r"(?:instagram|ig|insta)\s*[:@-]\s*@?([a-zA-Z0-9._]{2,30})", html_text, re.IGNORECASE)
        for m in text_matches:
            uname = m.group(1)
            if uname.lower() not in IG_RESERVED and not uname.lower().endswith((".com", ".net", ".org", ".png", ".jpg", ".js", ".css")):
                ig_candidates.append((3, uname, f"https://www.instagram.com/{uname}/"))

    if ig_candidates:
        ig_candidates.sort(key=lambda x: x[0], reverse=True)
        instagram = ig_candidates[0][2]

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

        soup, html_text = get_soup(url)
        e1, p1, ig1 = _scan_soup(soup, html_text)
        emails |= e1
        phones |= p1
        if not instagram and ig1:
            instagram = ig1

        # A page needs at least TWO of the three (email/phone/instagram) to
        # count as "good enough" -- finding just one usually isn't enough to
        # actually reach the company, so it's worth paying for the slow
        # browser step to try to find a second (this applies to every
        # subpage, not just the homepage -- a footer Instagram link can be
        # JS-rendered on /contact or /about just as easily as on the
        # homepage). Finding all 3 is a bonus, not required.
        # Instagram is the top priority -- keep trying the slow render step
        # for it specifically even if email+phone are already found, rather
        # than settling early. Still bounded to at most 2 pages total
        # (homepage + contact), so this doesn't reopen the memory-usage
        # problem from checking many pages.
        found_types = sum([bool(e1), bool(p1), bool(ig1)])
        should_render = (not ig1 and not instagram) or found_types < 2
        if should_render:
            soup_r, html_r = get_soup_rendered(url)
            e2, p2, ig2 = _scan_soup(soup_r, html_r)
            emails |= e2
            phones |= p2
            if not instagram and ig2:
                instagram = ig2

        # If the first couple of pages (each already given a real chance,
        # including the slow browser step) turned up absolutely nothing,
        # it's unlikely later guessed pages will suddenly succeed --
        # most companies in a typical batch have no findable contact info
        # at all, so giving up early here is what keeps the overall batch
        # fast instead of every "empty" company eating its full time budget.
        pages_checked = SUBPAGES_TO_CHECK.index(path) + 1
        if pages_checked >= 2 and not (emails or phones or instagram):
            break

        if instagram and sum([bool(emails), bool(phones), bool(instagram)]) >= 2:
            break  # Instagram secured, plus at least one more -- good enough

        time.sleep(DELAY_BETWEEN_PAGES)

    return list(emails), list(phones), instagram

