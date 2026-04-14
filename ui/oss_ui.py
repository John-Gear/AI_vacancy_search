OSS_UI_HTML = '''
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>AI vacancy search</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 1100px; margin: 30px auto; line-height: 1.5; }
    h1 { margin-bottom: 16px; }
    h2 { margin-top: 28px; }
    textarea, input {
      width: 100%;
      box-sizing: border-box;
      padding: 10px;
      border: 1px solid #ccc;
      border-radius: 8px;
      font: inherit;
    }
    textarea { min-height: 190px; resize: vertical; }
    .grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }
    .card {
      border: none;
      background: #fafafa;
      padding: 16px;
      background: #fafafa;
    }
    .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    button {
      padding: 12px 18px;
      border: 1px solid #ccc;
      border-radius: 10px;
      cursor: pointer;
      background: white;
    }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .spinner {
      width: 16px;
      height: 16px;
      border: 2px solid #ddd;
      border-top-color: #333;
      border-radius: 50%;
      display: none;
      animation: spin 0.8s linear infinite;
    }
    .spinner.active { display: inline-block; }
    .muted { color: #666; }
    .recommendation {
      border: 1px solid #ddd;
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 12px;
      background: white;
    }
    .score {
      display: inline-block;
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid #ccc;
      margin-left: 8px;
      font-size: 14px;
    }
    .box {
      background: #f6f6f6;
      border: 1px solid #ddd;
      padding: 12px;
      margin-top: 12px;
      white-space: pre-wrap;
      border-radius: 8px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <h1>AI vacancy search</h1>

  <div class="card">
    <div class="row">
      <button id="run-btn">Запустить анализ</button>
      <span id="spinner" class="spinner"></span>
      <span id="status-text" class="muted">Готов</span>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <label for="keywords"><b>Ключевые слова для поиска (введите свои позиции через запятую)</b></label>
      <input id="keywords" placeholder="ml,ds,llm,rag,data scientist,machine learning" />
    </div>

    <div class="card">
      <label for="profile_text"><b>Профиль / стек (опишите свой профиль и стек)</b></label>
      <textarea id="profile_text" placeholder="Опишите свой профиль и стек"></textarea>
    </div>

    <div class="card">
      <label for="system_prompt_text"><b>System prompt (промт для LLM по которому она оценивает вакансию. Менять только если уверены!)</b></label>
      <textarea id="system_prompt_text" placeholder="Системный промт"></textarea>
    </div>

    <div class="card">
      <label for="blacklist_text"><b>Blacklist (ссылки на вакансии, на которые откликнулись или невалидные/нерелевантные)</b></label>
      <textarea id="blacklist_text" placeholder="По одной ссылке на строку. Невалидные строки будут проигнорированы."></textarea>
    </div>
  </div>

  <h2>Сводка</h2>
  <div id="summary" class="box">Пока пусто</div>

  <h2>Рекомендации</h2>
  <div id="recommendations"></div>

  <script>
    const runBtn = document.getElementById("run-btn");
    const spinner = document.getElementById("spinner");
    const statusText = document.getElementById("status-text");
    const summaryBox = document.getElementById("summary");
    const recommendationsBox = document.getElementById("recommendations");

    function setBusy(isBusy, text = "Готов") {
      runBtn.disabled = isBusy;
      spinner.classList.toggle("active", isBusy);
      statusText.textContent = text;
    }

    function renderRecommendations(items) {
      recommendationsBox.innerHTML = "";

      if (!items || !items.length) {
        recommendationsBox.innerHTML = '<div class="box">Релевантных вакансий не найдено</div>';
        return;
      }

      const firstItems = items.slice(0, 20);
      const hiddenItems = items.slice(20);

      function createCard(item) {
        const div = document.createElement("div");
        div.className = "recommendation";
        div.innerHTML = `
          <div>
            <b>${item.title}</b>
            <span class="score">${item.fit_score}/10</span>
          </div>
          <div style="margin-top:8px;">
            <a href="${item.url}" target="_blank" rel="noopener noreferrer">${item.url}</a>
          </div>
          <div style="margin-top:8px;">${item.short_comment || ""}</div>
          <div class="muted" style="margin-top:8px;">matched_keywords: ${(item.matched_keywords || []).join(", ")}</div>
        `;
        return div;
      }

      firstItems.forEach(item => recommendationsBox.appendChild(createCard(item)));

      if (hiddenItems.length > 0) {
        const toggleBtn = document.createElement("button");
        toggleBtn.textContent = `Показать еще ${hiddenItems.length}`;
        toggleBtn.style.marginTop = "12px";

        const hiddenWrap = document.createElement("div");
        hiddenWrap.style.display = "none";
        hiddenWrap.style.marginTop = "12px";

        hiddenItems.forEach(item => hiddenWrap.appendChild(createCard(item)));

        toggleBtn.addEventListener("click", () => {
          const isHidden = hiddenWrap.style.display === "none";
          hiddenWrap.style.display = isHidden ? "block" : "none";
          toggleBtn.textContent = isHidden
            ? "Скрыть дополнительные вакансии"
            : `Показать еще ${hiddenItems.length}`;
        });

        recommendationsBox.appendChild(toggleBtn);
        recommendationsBox.appendChild(hiddenWrap);
      }
    }

    async function loadDefaults() {
      const resp = await fetch("/api/config");
      const data = await resp.json();
      document.getElementById("profile_text").value = data.default_profile_text || "";
      document.getElementById("system_prompt_text").value = data.default_system_prompt || "";
      document.getElementById("keywords").value = data.default_keywords || "";
    }

    async function runAnalysis() {
      const profileText = document.getElementById("profile_text").value.trim();
      const systemPromptText = document.getElementById("system_prompt_text").value.trim();
      const keywords = document.getElementById("keywords").value.trim();
      const blacklistText = document.getElementById("blacklist_text").value;

      if (!profileText) {
        alert("Поле Профиль / стек пустое");
        return;
      }
      if (!systemPromptText) {
        alert("Поле System prompt пустое");
        return;
      }

      setBusy(true, "Идет парсинг и анализ вакансий...");

      try {
        const resp = await fetch("/api/analyze/yandex", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            profile_text: profileText,
            system_prompt_text: systemPromptText,
            keywords: keywords,
            blacklist_text: blacklistText
          })
        });

        const data = await resp.json();

        if (!resp.ok) {
          summaryBox.textContent = data.error || "Ошибка";
          recommendationsBox.innerHTML = "";
          return;
        }

        const analyzed = data.stats?.unique_jobs ?? 0;
        const ignored = (data.stats?.skipped_blacklist ?? 0) + (data.blacklist?.ignored_count ?? 0);
        const hasErrors = ((data.errors?.fetch_errors?.length ?? 0) > 0) || ((data.errors?.llm_errors?.length ?? 0) > 0);

        summaryBox.textContent =
          `Проанализировано: ${analyzed}\n` +
          `Проигнорировано: ${ignored}\n` +
          `Ошибки: ${hasErrors ? "да" : "нет"}`;

        renderRecommendations(data.recommendations);
      } catch (e) {
        summaryBox.textContent = String(e);
        recommendationsBox.innerHTML = "";
      } finally {
        setBusy(false, "Готов");
      }
    }

    runBtn.addEventListener("click", runAnalysis);
    loadDefaults();
  </script>
</body>
</html>
'''
