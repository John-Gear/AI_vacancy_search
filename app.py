import os
import re
import json
import time
from html import unescape

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request

from parsers.yandex_jobs import fetch_jobs_for_keyword
from ui.oss_ui import OSS_UI_HTML

load_dotenv()

REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))
LLM_BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "10"))
REPORT_MIN_SCORE = int(os.getenv("REPORT_MIN_SCORE", "7"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

DEFAULT_PROFILE_TEXT = os.getenv(
    "DEFAULT_PROFILE_TEXT",
    "Кандидат: AI Product Engineer / ML Developer. "
    "Сильные стороны: Python, SQL, pandas, Numpy, Scikit-learn, CatBoost, LightGBM, TensorFlow, Docker, Git, Linux, FastAPI, Flask, RAG, LLM, Prompt Engineering, Docker Compose "
    "Ключевые компетенции: "
    "1. Backend & DevOps: Проектирование микросервисов (FastAPI/Flask), контейнеризация(Docker, Docker Compose), администрирование Linux-серверов. "
    "2. Machine Learning: Полный цикл разработки моделей и их инференс (EDA, Feature Engineering, обучение CatBoost/LightGBM, валидация бизнес-метрик, упаковка в Docker и реализация API на Flask). "
    "3. Product Mindset: Опыт руководства командой (5 человек) и запуска сложных IT-продуктов для крупных государственных и коммерческих заказчиков (Мосгортранс, Газэнергострой и др.). "
)

DEFAULT_SYSTEM_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    "Ты оцениваешь вакансии относительно профиля кандидата. "
    "Твоя задача: "
    "1. Для каждой вакансии оценить соответствие профилю кандидата по шкале от 0 до 10. "
    "2. Учитывать стек, тип задач, инженерную направленность, уровень позиции. "
    "3. Если роль сильно senior/lead/head и явно выше уровня кандидата — снижай оценку. "
    "4. Если стек и задачи совпадают, но уровень чуть выше — вакансия все еще может быть релевантной. "
    "5. Отвечай строго JSON-массивом без пояснений вне JSON. "
    "6. Отвечай развернуто, например так: 'Совпадает по Python, LLM и прикладному ML, но роль ближе к middle. Откликнуться стоит. Релевантно по ML backend, но мало совпадений с LLM/RAG-фокусом.' "
    'Формат ответа:[{"url": "string", "fit_score": 0, "should_apply": true, "short_comment": "краткий комментарий на русском, 1-2 предложения"}] '
)
DEFAULT_KEYWORDS = os.getenv("YANDEX_JOB_KEYWORDS", "ml,ds,llm,rag,data scientist,machine learning")
VACANCY_URL_PATTERN = os.getenv(
    "VACANCY_URL_PATTERN",
    r"^https://yandex\.ru/jobs/vacancies/[A-Za-z0-9\-]+$"
)

app = Flask(__name__)


def clean_text(text: str) -> str:
    text = text or ""
    text = unescape(text)
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def fetch_html(session: requests.Session, url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    resp = session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def remove_benefits_block(text: str) -> str:
    patterns = [
        r"\nЧто мы предлагаем[\s\S]*$",
        r"\nМы предлагаем[\s\S]*$",
        r"\nУсловия[\s\S]*$",
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


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def parse_keywords(raw_keywords: str) -> list[str]:
    return [x.strip() for x in (raw_keywords or "").split(",") if x.strip()]


def parse_blacklist(raw_blacklist: str) -> tuple[set[str], list[str]]:
    valid = set()
    ignored = []

    for line in (raw_blacklist or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(VACANCY_URL_PATTERN, line):
            valid.add(line)
        else:
            ignored.append(line)

    return valid, ignored


def merge_jobs(jobs: list[dict]) -> list[dict]:
    merged = {}

    for job in jobs:
        url = job["url"]
        if url not in merged:
            merged[url] = {
                "url": url,
                "title": job["title"],
                "description": job["description"],
                "keyword": job["keyword"],
                "matched_keywords": list(job.get("matched_keywords", [])),
            }
        else:
            existing = set(merged[url]["matched_keywords"])
            existing.update(job.get("matched_keywords", []))
            merged[url]["matched_keywords"] = sorted(existing)

    return list(merged.values())


def call_openrouter(batch_jobs: list[dict], profile_text: str, system_prompt_text: str) -> list[dict]:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    user_prompt = f"""
Профиль кандидата:
{profile_text}

Оцени следующие вакансии.
Верни только JSON-массив.

Вакансии:
{json.dumps(batch_jobs, ensure_ascii=False, indent=2)}
""".strip()

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt_text},
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
    content = re.sub(r"^```json\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"^```\s*", "", content)
    content = re.sub(r"\s*```$", "", content)

    parsed = json.loads(content)
    if not isinstance(parsed, list):
        raise ValueError("LLM response is not a JSON list")

    return parsed


def analyze_yandex_jobs(
    keywords: list[str],
    profile_text: str,
    system_prompt_text: str,
    blacklist_urls: set[str],
) -> dict:
    session = requests.Session()

    all_jobs = []
    cards_seen_total = 0
    skipped_blacklist = 0
    fetch_errors = []

    for keyword in keywords:
        try:
            cards = fetch_jobs_for_keyword(session, keyword)
        except Exception as e:
            fetch_errors.append(f"keyword={keyword}: {e}")
            continue

        cards_seen_total += len(cards)

        for card in cards:
            if card["url"] in blacklist_urls:
                skipped_blacklist += 1
                continue

            try:
                time.sleep(REQUEST_DELAY)
                html = fetch_html(session, card["url"])
                job = parse_vacancy_page(html, card["url"], keyword)
                all_jobs.append(job)
            except Exception as e:
                fetch_errors.append(f"vacancy={card['url']}: {e}")

    unique_jobs = merge_jobs(all_jobs)

    llm_results = []
    llm_errors = []

    for batch in chunked(unique_jobs, LLM_BATCH_SIZE):
        try:
            result = call_openrouter(batch, profile_text, system_prompt_text)
            llm_results.extend(result)
        except Exception as e:
            llm_errors.append(str(e))

    scored_by_url = {}
    for item in llm_results:
        url = item.get("url")
        if not url:
            continue
        scored_by_url[url] = {
            "fit_score": int(item.get("fit_score", 0)),
            "should_apply": bool(item.get("should_apply", False)),
            "short_comment": clean_text(item.get("short_comment", "")),
        }

    recommendations = []
    for job in unique_jobs:
        score = scored_by_url.get(job["url"])
        if not score:
            continue
        if score["fit_score"] < REPORT_MIN_SCORE:
            continue

        recommendations.append({
            "title": job["title"],
            "url": job["url"],
            "fit_score": score["fit_score"],
            "should_apply": score["should_apply"],
            "short_comment": score["short_comment"],
            "matched_keywords": job["matched_keywords"],
        })

    recommendations.sort(key=lambda x: (-x["fit_score"], x["title"]))

    return {
        "keywords": keywords,
        "stats": {
            "cards_seen_total": cards_seen_total,
            "jobs_after_blacklist": len(all_jobs),
            "unique_jobs": len(unique_jobs),
            "skipped_blacklist": skipped_blacklist,
            "llm_processed": len(llm_results),
            "recommendations_count": len(recommendations),
        },
        "errors": {
            "fetch_errors": fetch_errors,
            "llm_errors": llm_errors,
        },
        "recommendations": recommendations,
    }


@app.route("/", methods=["GET"])
def oss_ui():
    return OSS_UI_HTML


@app.route("/api/config", methods=["GET"])
def api_config():
    return jsonify({
        "default_profile_text": DEFAULT_PROFILE_TEXT,
        "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
        "default_keywords": DEFAULT_KEYWORDS,
        "vacancy_url_pattern": VACANCY_URL_PATTERN,
        "report_min_score": REPORT_MIN_SCORE,
        "llm_batch_size": LLM_BATCH_SIZE,
    })


@app.route("/api/analyze/yandex", methods=["POST"])
def api_analyze_yandex():
    data = request.get_json(silent=True) or {}

    profile_text = clean_text(data.get("profile_text", ""))
    system_prompt_text = clean_text(data.get("system_prompt_text", ""))
    raw_keywords = data.get("keywords", DEFAULT_KEYWORDS)
    raw_blacklist = data.get("blacklist_text", "")

    if not profile_text:
        return jsonify({"error": "Поле profile_text пустое"}), 400
    if not system_prompt_text:
        return jsonify({"error": "Поле system_prompt_text пустое"}), 400

    keywords = parse_keywords(raw_keywords)
    if not keywords:
        return jsonify({"error": "Поле keywords пустое"}), 400

    blacklist_urls, ignored_blacklist_lines = parse_blacklist(raw_blacklist)

    result = analyze_yandex_jobs(
        keywords=keywords,
        profile_text=profile_text,
        system_prompt_text=system_prompt_text,
        blacklist_urls=blacklist_urls,
    )

    result["blacklist"] = {
        "valid_count": len(blacklist_urls),
        "ignored_count": len(ignored_blacklist_lines),
        "ignored_lines": ignored_blacklist_lines,
    }
    return jsonify(result)


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host=host, port=port, debug=False, use_reloader=False)
