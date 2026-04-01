import os
import re
import json
import time
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


BASE_URL = "https://yandex.ru"
SEARCH_URL = "https://yandex.ru/jobs/vacancies?text={query}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def get_env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def fetch_html(session: requests.Session, url: str) -> str:
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_search_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    # Основной вариант: ищем карточки по классу, который ты указал
    cards = soup.select(".lc-jobs-vacancy-card")

    if cards:
        for card in cards:
            link = card.find("a", href=True)
            if not link:
                continue

            href = link["href"].strip()
            full_url = urljoin(BASE_URL, href)

            if full_url in seen:
                continue
            seen.add(full_url)

            title = clean_text(link.get_text(" ", strip=True))
            card_text = clean_text(card.get_text(" ", strip=True))

            results.append({
                "url": full_url,
                "title": title,
                "card_text": card_text,
            })
        return results

    # Фолбэк: если класс поменяется, просто собираем все ссылки /jobs/vacancies/
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("/jobs/vacancies/"):
            continue

        full_url = urljoin(BASE_URL, href)
        if full_url in seen:
            continue
        seen.add(full_url)

        title = clean_text(a.get_text(" ", strip=True))
        if not title:
            continue

        results.append({
            "url": full_url,
            "title": title,
            "card_text": "",
        })

    return results


def parse_vacancy_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))

    # Основной блок описания по классу, который ты указал
    description_blocks = soup.select(".lc-jobs-vacancy-mvp__description")

    description_parts = []
    for block in description_blocks:
        text = clean_text(block.get_text("\n", strip=True))
        if text:
            description_parts.append(text)

    # Фолбэк: если класс не найден, берем основной текст страницы
    if not description_parts:
        main_candidates = []
        for tag in soup.find_all(["main", "article", "section", "div"]):
            text = clean_text(tag.get_text(" ", strip=True))
            if len(text) > 500:
                main_candidates.append(text)

        if main_candidates:
            description_parts.append(max(main_candidates, key=len))

    description = "\n\n".join(description_parts).strip()

    return {
        "url": url,
        "title": title,
        "description": description,
    }


def main():
    load_dotenv()

    keywords = get_env_list("YANDEX_JOB_KEYWORDS", "ml,ds")
    out_file = os.getenv("OUTPUT_FILE", "yandex_jobs.jsonl")
    delay = float(os.getenv("REQUEST_DELAY", "1.0"))

    session = requests.Session()

    all_items = []
    seen_urls = set()

    for keyword in keywords:
        search_url = SEARCH_URL.format(query=quote_plus(keyword))
        print(f"[SEARCH] {keyword}: {search_url}")

        try:
            html = fetch_html(session, search_url)
            cards = parse_search_page(html)
            print(f"  найдено карточек: {len(cards)}")
        except Exception as e:
            print(f"  ошибка поиска '{keyword}': {e}")
            continue

        for card in cards:
            url = card["url"]
            if url in seen_urls:
                continue

            seen_urls.add(url)
            time.sleep(delay)

            try:
                vacancy_html = fetch_html(session, url)
                vacancy = parse_vacancy_page(vacancy_html, url)
                vacancy["keyword"] = keyword
                vacancy["card_title"] = card.get("title", "")
                vacancy["card_text"] = card.get("card_text", "")
                all_items.append(vacancy)

                print(f"  OK: {vacancy['title'] or url}")
            except Exception as e:
                print(f"  ошибка вакансии {url}: {e}")

    with open(out_file, "w", encoding="utf-8") as f:
        for item in all_items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"\nСохранено {len(all_items)} вакансий в {out_file}")


if __name__ == "__main__":
    main()