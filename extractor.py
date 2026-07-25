"""
Extraction engine: given a company website, finds email, phone, and Instagram.
Kept separate from app.py so it can be tested or reused independently.
"""

import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SUBPAGES_TO_CHECK = ["", "/contact", "/contact-us", "/about", "/about-us"]
REQUEST_TIMEOUT = 10
DELAY_BETWEEN_PAGES = 0.6

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(r"(\+?\d[\d\-.\s()]{8,}\d)")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}


def get_soup(url):
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
            return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException:
        return None
    return None


def clean_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return None
    return raw.strip()


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
        if not soup:
            time.sleep(DELAY_BETWEEN_PAGES)
            continue

        text = soup.get_text(" ")
        for e in EMAIL_REGEX.findall(text):
            if not e.lower().endswith(IMAGE_EXT):
                emails.add(e.lower())

        for p in PHONE_REGEX.findall(text):
            cp = clean_phone(p)
            if cp:
                phones.add(cp)

        if not instagram:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "instagram.com" in href and "/p/" not in href:
                    instagram = href.split("?")[0]
                    break

        time.sleep(DELAY_BETWEEN_PAGES)

    return list(emails), list(phones), instagram
