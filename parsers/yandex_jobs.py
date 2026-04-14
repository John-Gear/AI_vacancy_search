import os
import re
from html import unescape
from urllib.parse import urljoin

import requests

BASE_URL = "https://yandex.ru"
PUBLICATIONS_API_URL = "https://yandex.ru/jobs/api/publications"
API_PAGE_SIZE = int(os.getenv("API_PAGE_SIZE", "20"))
MAX_RESULTS_PER_KEYWORD = int(os.getenv("MAX_RESULTS_PER_KEYWORD", "50"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def clean_text(text: str) -> str:
    text = text or ""
    text = unescape(text)
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def strip_html(text: str) -> str:
    text = text or ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


def fetch_json(session: requests.Session, url: str, params: dict | None = None) -> dict:
    resp = session.get(url, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalize_next_url(next_url: str | None) -> str | None:
    if not next_url:
        return None
    if next_url.startswith("http://femida.yandex-team.ru/_api/jobs/publications/"):
        return next_url.replace(
            "http://femida.yandex-team.ru/_api/jobs/publications/",
            "https://yandex.ru/jobs/api/publications"
        )
    return next_url


def fetch_jobs_for_keyword(session: requests.Session, keyword: str, limit: int | None = None) -> list[dict]:
    limit = limit or MAX_RESULTS_PER_KEYWORD
    items = []
    seen = set()

    next_url = PUBLICATIONS_API_URL
    params = {
        "page_size": API_PAGE_SIZE,
        "text": keyword,
    }

    while next_url and len(items) < limit:
        data = fetch_json(session, next_url, params=params)
        params = None

        for row in data.get("results", []):
            slug = row.get("publication_slug_url")
            if not slug:
                continue

            url = urljoin(BASE_URL, f"/jobs/vacancies/{slug}")
            if url in seen:
                continue
            seen.add(url)

            title = strip_html(row.get("title") or "") or "Без названия"
            items.append({
                "url": url,
                "title": title,
                "source": "yandex",
            })

            if len(items) >= limit:
                break

        next_url = normalize_next_url(data.get("next"))

    return items[:limit]
