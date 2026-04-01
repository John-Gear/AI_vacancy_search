import os
import re
import json
import time
import sqlite3
import hashlib
from datetime import datetime
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, request, redirect, url_for, render_template_string
from datetime import datetime, timezone

load_dotenv()

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

DATABASE_PATH = os.getenv("DATABASE_PATH", "jobs.db")
REPORTS_DIR = os.getenv("REPORTS_DIR", "reports")
PROFILE_PATH = os.getenv("PROFILE_PATH", "profile.txt")
SYSTEM_PROMPT_PATH = os.getenv("SYSTEM_PROMPT_PATH", "system_prompt.txt")
BLACKLIST_PATH = os.getenv("BLACKLIST_PATH", "blacklist.txt")

MAX_RESULTS_PER_KEYWORD = int(os.getenv("MAX_RESULTS_PER_KEYWORD", "50"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))
LLM_BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "10"))
REPORT_MIN_SCORE = int(os.getenv("REPORT_MIN_SCORE", "7"))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

os.makedirs(REPORTS_DIR, exist_ok=True)

app = Flask(__name__)


# -----------------------------
# DB
# -----------------------------
def get_conn():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        keyword TEXT,
        matched_keywords TEXT,
        description TEXT,
        content_hash TEXT,
        first_seen_at TEXT,
        last_seen_at TEXT,
        is_processed INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS llm_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_url TEXT NOT NULL UNIQUE,
        fit_score INTEGER,
        should_apply INTEGER,
        short_comment TEXT,
        processed_at TEXT,
        model TEXT,
        FOREIGN KEY(job_url) REFERENCES jobs(url)
    )
    """)

    conn.commit()
    conn.close()


# -----------------------------
# Utils
# -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def content_hash(title: str, description: str) -> str:
    raw = f"{title}\n{description}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def get_keywords() -> list[str]:
    raw = os.getenv("YANDEX_JOB_KEYWORDS", "ml,ds,llm")
    return [x.strip() for x in raw.split(",") if x.strip()]


def read_text_file(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def read_blacklist() -> set[str]:
    if not os.path.exists(BLACKLIST_PATH):
        return set()
    with open(BLACKLIST_PATH, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def fetch_html(session: requests.Session, url: str) -> str:
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


# -----------------------------
# Parsing
# -----------------------------
def parse_search_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()

    cards = soup.select(".lc-jobs-vacancy-card")
    for card in cards:
        link = card.find("a", href=True)
        if not link:
            continue
        href = link["href"].strip()
        if not href.startswith("/jobs/vacancies/"):
            continue

        full_url = urljoin(BASE_URL, href)
        if full_url in seen:
            continue
        seen.add(full_url)

        title = clean_text(link.get_text(" ", strip=True))
        if not title:
            title = "Без названия"

        items.append({
            "url": full_url,
            "title": title,
        })

    # fallback
    if not items:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("/jobs/vacancies/"):
                continue
            full_url = urljoin(BASE_URL, href)
            if full_url in seen:
                continue
            seen.add(full_url)
            title = clean_text(a.get_text(" ", strip=True)) or "Без названия"
            items.append({
                "url": full_url,
                "title": title,
            })

    return items


def remove_benefits_block(text: str) -> str:
    """
    Мягко отрезаем блоки типа 'Что мы предлагаем', если они есть.
    """
    patterns = [
        r"\nЧто мы предлагаем[\s\S]*$",
        r"\nМы предлагаем[\s\S]*$",
        r"\nУсловия[\s\S]*$",
        r"\nБудет плюсом[\s\S]*$",
    ]
    result = text
    for pattern in patterns:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    return clean_text(result)


def parse_vacancy_page(html: str, url: str, keyword: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))

    blocks = soup.select(".lc-jobs-vacancy-mvp__description")
    description_parts = []

    for block in blocks:
        txt = clean_text(block.get_text("\n", strip=True))
        if txt:
            description_parts.append(txt)

    if not description_parts:
        # fallback: берем длинный текст из main/article/section/div
        candidates = []
        for tag in soup.find_all(["main", "article", "section", "div"]):
            txt = clean_text(tag.get_text(" ", strip=True))
            if len(txt) > 500:
                candidates.append(txt)
        if candidates:
            description_parts.append(max(candidates, key=len))

    description = "\n\n".join(description_parts)
    description = remove_benefits_block(description)

    return {
        "url": url,
        "title": title or "Без названия",
        "keyword": keyword,
        "matched_keywords": [keyword],
        "description": description,
    }


def fetch_jobs_for_keyword(session: requests.Session, keyword: str, limit: int) -> list[dict]:
    """
    Пока без сложной пагинации: берем то, что есть на текущей выдаче,
    потом обрежем до limit.
    """
    search_url = SEARCH_URL.format(query=quote_plus(keyword))
    html = fetch_html(session, search_url)
    cards = parse_search_page(html)
    return cards[:limit]


# -----------------------------
# DB Ops
# -----------------------------
def get_existing_jobs_map() -> dict[str, sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM jobs").fetchall()
    conn.close()
    return {row["url"]: row for row in rows}


def upsert_job(job: dict):
    conn = get_conn()
    cur = conn.cursor()

    existing = cur.execute(
        "SELECT * FROM jobs WHERE url = ?",
        (job["url"],)
    ).fetchone()

    current_time = now_iso()

    if existing:
        existing_keywords = set(json.loads(existing["matched_keywords"] or "[]"))
        new_keywords = set(job.get("matched_keywords", []))
        merged_keywords = sorted(existing_keywords | new_keywords)

        cur.execute("""
            UPDATE jobs
            SET matched_keywords = ?, last_seen_at = ?
            WHERE url = ?
        """, (
            json.dumps(merged_keywords, ensure_ascii=False),
            current_time,
            job["url"]
        ))
    else:
        cur.execute("""
            INSERT INTO jobs (
                url, title, keyword, matched_keywords, description,
                content_hash, first_seen_at, last_seen_at, is_processed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            job["url"],
            job["title"],
            job["keyword"],
            json.dumps(job.get("matched_keywords", []), ensure_ascii=False),
            job["description"],
            content_hash(job["title"], job["description"]),
            current_time,
            current_time,
        ))

    conn.commit()
    conn.close()


def remove_jobs_not_in_search(active_urls: set[str]):
    conn = get_conn()
    cur = conn.cursor()

    rows = cur.execute("SELECT url FROM jobs").fetchall()
    db_urls = {row["url"] for row in rows}

    to_delete = db_urls - active_urls

    for url in to_delete:
        cur.execute("DELETE FROM llm_results WHERE job_url = ?", (url,))
        cur.execute("DELETE FROM jobs WHERE url = ?", (url,))

    conn.commit()
    conn.close()
    return len(to_delete)


def remove_blacklisted_jobs():
    blacklist = read_blacklist()
    if not blacklist:
        return 0

    conn = get_conn()
    cur = conn.cursor()
    removed = 0

    for url in blacklist:
        cur.execute("DELETE FROM llm_results WHERE job_url = ?", (url,))
        deleted = cur.execute("DELETE FROM jobs WHERE url = ?", (url,)).rowcount
        removed += deleted

    conn.commit()
    conn.close()
    return removed


def get_unprocessed_jobs(limit: int | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    sql = "SELECT * FROM jobs WHERE is_processed = 0 ORDER BY id ASC"
    if limit is not None:
        rows = conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


def save_llm_results(results: list[dict]):
    conn = get_conn()
    cur = conn.cursor()

    for item in results:
        url = item["url"]
        fit_score = int(item.get("fit_score", 0))
        should_apply = 1 if item.get("should_apply", False) else 0
        short_comment = item.get("short_comment", "").strip()
        processed_at = now_iso()

        cur.execute("""
            INSERT OR REPLACE INTO llm_results (
                job_url, fit_score, should_apply, short_comment, processed_at, model
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            url,
            fit_score,
            should_apply,
            short_comment,
            processed_at,
            OPENROUTER_MODEL,
        ))

        cur.execute("""
            UPDATE jobs
            SET is_processed = 1
            WHERE url = ?
        """, (url,))

    conn.commit()
    conn.close()


# -----------------------------
# OpenRouter
# -----------------------------
def build_llm_payload(batch_jobs: list[sqlite3.Row], profile: str, system_prompt: str) -> list[dict]:
    jobs_for_prompt = []
    for row in batch_jobs:
        jobs_for_prompt.append({
            "url": row["url"],
            "title": row["title"],
            "description": row["description"],
            "matched_keywords": json.loads(row["matched_keywords"] or "[]"),
        })
    return jobs_for_prompt


def call_openrouter(batch_jobs: list[sqlite3.Row]) -> list[dict]:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    profile = read_text_file(PROFILE_PATH)
    system_prompt = read_text_file(SYSTEM_PROMPT_PATH)

    jobs_payload = build_llm_payload(batch_jobs, profile, system_prompt)

    user_prompt = f"""
Профиль кандидата:
{profile}

Оцени следующие вакансии.
Верни только JSON-массив.

Вакансии:
{json.dumps(jobs_payload, ensure_ascii=False, indent=2)}
""".strip()

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()

    content = data["choices"][0]["message"]["content"].strip()

    # На случай если модель обернет JSON в ```json
    content = re.sub(r"^```json\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"^```\s*", "", content)
    content = re.sub(r"\s*```$", "", content)

    parsed = json.loads(content)
    if not isinstance(parsed, list):
        raise ValueError("LLM response is not a JSON list")

    return parsed


# -----------------------------
# Pipeline
# -----------------------------
def run_parser() -> dict:
    session = requests.Session()
    keywords = get_keywords()

    active_urls = set()
    found_count = 0
    added_or_updated = 0

    for keyword in keywords:
        try:
            cards = fetch_jobs_for_keyword(session, keyword, MAX_RESULTS_PER_KEYWORD)
        except Exception as e:
            print(f"[ERROR] keyword={keyword}: {e}")
            continue

        for card in cards:
            url = card["url"]
            active_urls.add(url)

            try:
                time.sleep(REQUEST_DELAY)
                html = fetch_html(session, url)
                job = parse_vacancy_page(html, url, keyword)
                upsert_job(job)
                found_count += 1
                added_or_updated += 1
            except Exception as e:
                print(f"[ERROR] vacancy={url}: {e}")

    removed_closed = remove_jobs_not_in_search(active_urls)
    removed_blacklist = remove_blacklisted_jobs()

    return {
        "keywords": keywords,
        "found_count": found_count,
        "added_or_updated": added_or_updated,
        "removed_closed": removed_closed,
        "removed_blacklist": removed_blacklist,
    }


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def run_llm() -> dict:
    jobs = get_unprocessed_jobs()
    if not jobs:
        return {
            "unprocessed": 0,
            "processed_batches": 0,
            "processed_jobs": 0,
        }

    processed_batches = 0
    processed_jobs = 0

    for batch in chunked(jobs, LLM_BATCH_SIZE):
        try:
            result = call_openrouter(batch)
            save_llm_results(result)
            processed_batches += 1
            processed_jobs += len(batch)
        except Exception as e:
            print(f"[LLM ERROR] {e}")

    return {
        "unprocessed": len(jobs),
        "processed_batches": processed_batches,
        "processed_jobs": processed_jobs,
    }


def build_report() -> dict:
    conn = get_conn()

    rows = conn.execute("""
        SELECT
            j.title,
            j.url,
            r.fit_score,
            r.should_apply,
            r.short_comment
        FROM jobs j
        JOIN llm_results r ON j.url = r.job_url
        ORDER BY r.fit_score DESC, j.title ASC
    """).fetchall()

    conn.close()

    filtered = [row for row in rows if row["fit_score"] is not None and row["fit_score"] >= REPORT_MIN_SCORE]

    report_lines = []
    for idx, row in enumerate(filtered, start=1):
        report_lines.append(
            f"{idx}. {row['title']}\n"
            f"URL: {row['url']}\n"
            f"Соответствие: {row['fit_score']}/10\n"
            f"Комментарий: {row['short_comment']}\n"
        )

    report_lines.append(f"\nВсего релевантных вакансий: {len(filtered)}")

    report_text = "\n".join(report_lines).strip()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    report_path = os.path.join(REPORTS_DIR, f"weekly_report_{ts}.txt")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    return {
        "report_path": report_path,
        "count": len(filtered),
    }


# -----------------------------
# UI
# -----------------------------
HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Job Hunter MVP</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 900px; margin: 40px auto; line-height: 1.5; }
    h1 { margin-bottom: 24px; }
    .buttons { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
    form { display: inline; }
    button { padding: 12px 18px; cursor: pointer; }
    .box {
      background: #f6f6f6;
      border: 1px solid #ddd;
      padding: 16px;
      margin-top: 20px;
      white-space: pre-wrap;
    }
    table { border-collapse: collapse; width: 100%; margin-top: 24px; }
    th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
  </style>
</head>
<body>
  <h1>Job Hunter MVP</h1>

  <div class="buttons">
    <form method="post" action="/run-parser">
      <button type="submit">1. Запустить парсер</button>
    </form>

    <form method="post" action="/run-llm">
      <button type="submit">2. Запустить обработку LLM</button>
    </form>

    <form method="post" action="/build-report">
      <button type="submit">3. Сформировать отчет</button>
    </form>
  </div>

  {% if message %}
    <div class="box">{{ message }}</div>
  {% endif %}

  <h2>Сводка по базе</h2>
  <table>
    <tr>
      <th>Показатель</th>
      <th>Значение</th>
    </tr>
    <tr><td>Всего вакансий</td><td>{{ stats.jobs_count }}</td></tr>
    <tr><td>Необработанных LLM</td><td>{{ stats.unprocessed_count }}</td></tr>
    <tr><td>Обработанных LLM</td><td>{{ stats.processed_count }}</td></tr>
  </table>
</body>
</html>
"""


def get_stats():
    conn = get_conn()
    jobs_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    unprocessed_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_processed = 0").fetchone()[0]
    processed_count = conn.execute("SELECT COUNT(*) FROM llm_results").fetchone()[0]
    conn.close()
    return {
        "jobs_count": jobs_count,
        "unprocessed_count": unprocessed_count,
        "processed_count": processed_count,
    }


@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML, message="", stats=get_stats())


@app.route("/run-parser", methods=["POST"])
def run_parser_route():
    result = run_parser()
    msg = (
        f"Парсер завершен.\n"
        f"Ключи: {', '.join(result['keywords'])}\n"
        f"Найдено/обновлено: {result['found_count']}\n"
        f"Удалено закрытых: {result['removed_closed']}\n"
        f"Удалено по blacklist: {result['removed_blacklist']}"
    )
    return render_template_string(HTML, message=msg, stats=get_stats())


@app.route("/run-llm", methods=["POST"])
def run_llm_route():
    result = run_llm()
    msg = (
        f"LLM-обработка завершена.\n"
        f"Необработанных было: {result['unprocessed']}\n"
        f"Обработано батчей: {result['processed_batches']}\n"
        f"Обработано вакансий: {result['processed_jobs']}"
    )
    return render_template_string(HTML, message=msg, stats=get_stats())


@app.route("/build-report", methods=["POST"])
def build_report_route():
    result = build_report()
    msg = (
        f"Отчет сформирован.\n"
        f"Файл: {result['report_path']}\n"
        f"Релевантных вакансий: {result['count']}"
    )
    return render_template_string(HTML, message=msg, stats=get_stats())


if __name__ == "__main__":
    init_db()
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host=host, port=port, debug=True)