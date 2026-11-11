const $ = (id) => document.getElementById(id);
const settings = $("settings");
const shopping = $("shopping");
const sortForm = $("shopping-form");
const benchForm = $("benchmark-form");
const departments = $("departments");
const table = $("bench-results");
const rows = [];
let active = false;
let benchmarkController;

const csvColumns = [
    "label",
    "model",
    "concurrency",
    "requests",
    "errors",
    "ttft_p50_s",
    "ttft_p95_s",
    "tokens_per_s_per_user_p50",
    "throughput_tokens_per_s",
    "reasoning",
    "ttft_p99_s",
    "tokens_per_s_per_user_p95",
    "tokens_per_s_per_user_p99",
];
const tableColumns = [
    ...csvColumns.slice(0, 5),
    "duration_s",
    "tokens_per_request",
    "ttft_p50_s",
    "ttft_p95_s",
    "ttft_p99_s",
    "tokens_per_s_per_user_p50",
    "tokens_per_s_per_user_p95",
    "tokens_per_s_per_user_p99",
    "throughput_tokens_per_s",
    "reasoning",
];
const shoppingPrompts = [
    "молоко, хліб, яблука, куряче філе, гречка",
    "лосось охолоджений; тунець консервований; заморожене філе хека; свіжа полуниця; заморожена полуниця",
    "яблука 1,5 кг; молоко 2,5% — 1 л; яблука 2 шт; яйця 10 шт",
    "зубна паста; пральний порошок; туалетний папір; корм для кота; батарейки",
    "газована вода, апельсиновий сік, кава мелена, чай чорний, оливкова олія, чорний шоколад",
    "лаваш; круасан свіжий; йогурт; вівсяне молоко; сир кисломолочний",
    "bananas, mozzarella, sparkling water, frozen peas, chicken breast, dish soap",
    "дрібничка, зошит, петрушка, печериці, пельмені заморожені",
    "бородинський хліб",
    "морква; картопля; груші; кріп; батон; булочка; яйця; масло вершкове; кефір; свинина; креветки охолоджені; ковбаса; рис; консервована кукурудза; цукор; кетчуп; морозиво; шпинат заморожений; піца заморожена; лимонад; мінеральна вода; серветки паперові; мило; корм для собак",
];
const textPrompts = [
    "Яка столиця Австралії?",
    "Поясни в трьох реченнях, що таке квантизація моделі.",
    "Напиши короткий лист викладачеві з проханням перенести здачу лабораторної на тиждень.",
    "Explain in two sentences why the sky is blue.",
    "Склади план підготовки до іспиту з програмування на 5 днів.",
    "Write a Python function that checks whether a string is a palindrome.",
    "Перелічи три переваги й три недоліки локального запуску моделей.",
    "Translate into Ukrainian: The meeting has been moved to Thursday.",
];

function message(id, text, error = false) {
    $(id).textContent = text;
    $(id).dataset.error = String(error);
}

function view(benchmark) {
    $("sort-view").hidden = benchmark;
    $("benchmark-view").hidden = !benchmark;
    $("show-sort").setAttribute("aria-pressed", String(!benchmark));
    $("show-benchmark").setAttribute("aria-pressed", String(benchmark));
}

$("show-sort").onclick = () => view(false);
$("show-benchmark").onclick = () => view(true);
settings.onsubmit = (event) => event.preventDefault();

function protocolFields() {
    const native = settings.elements.protocol.value === "ollama";
    for (const label of document.querySelectorAll(".native-only")) {
        label.hidden = !native;
        label.querySelector("input").disabled = !native;
    }
    $("settings-hint").textContent = native
        ? "" //"Порожні поля використовують налаштування Ollama. Low, Medium і High вмикають міркування для моделей, які його підтримують."
        : ""; //"Порожні поля використовують налаштування моделі. Top-k і контекст можна змінити через Ollama API. Для lab4-q4 та lab4-q8 контекст Modelfile — 4096.";
}
settings.elements.protocol.onchange = protocolFields;

async function refreshModels() {
    const select = settings.elements.model;
    const previous = select.value;
    try {
        const response = await fetch("/api/status");
        const status = await response.json();
        if (!response.ok || !status.models?.length) throw new Error();
        select.replaceChildren(...status.models.map((name) => new Option(name, name)));
        select.value = status.models.includes(previous) ? previous : status.ready ? status.model : status.models[0];
        $("model-status").textContent = "Ollama підключено";
        $("model-status").dataset.ready = "true";
    } catch {
        select.replaceChildren(new Option("Немає доступних моделей", ""));
        $("model-status").textContent = "Ollama або моделі недоступні";
        $("model-status").dataset.ready = "false";
    }
}
$("refresh-models").onclick = refreshModels;

function readSettings() {
    if (!settings.reportValidity()) return null;
    const data = Object.fromEntries(new FormData(settings));
    for (const input of settings.querySelectorAll('input[type="number"]')) {
        if (input.disabled) continue;
        data[input.name] = input.value === "" ? null : Number(input.value);
    }
    return data;
}

function busy(value) {
    active = value;
    $("submit").disabled = value;
    $("example").disabled = value;
    shopping.readOnly = value;
    benchForm.querySelector('button[type="submit"]').disabled = value;
    $("bench-clear").disabled = value;
}

async function stream(path, payload, onEvent, signal) {
    let response;
    try {
        response = await fetch(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            signal,
        });
    } catch (error) {
        if (signal?.aborted) throw error;
        throw new Error("Сервер сторінки недоступний. Перевір, чи запущено server.py.");
    }
    if (!response.ok) {
        const result = await response.json().catch(() => null);
        throw new Error(result?.error || "Сервер повернув некоректну відповідь.");
    }
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let pending = "";
    try {
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            pending += value;
            const lines = pending.split("\n");
            pending = lines.pop();
            for (const line of lines) {
                const event = JSON.parse(line);
                if (event.type === "error") throw new Error(event.message);
                onEvent(event);
            }
        }
        if (pending.trim()) throw new Error("Потік відповіді перервано.");
    } finally {
        await reader.cancel().catch(() => {});
    }
}

function updateInput() {
    $("count").textContent = `${shopping.value.length} / ${shopping.maxLength}`;
    shopping.setCustomValidity("");
}
shopping.oninput = updateInput;
$("example").onclick = () => {
    shopping.value =
        "яблука 1,5 кг, молоко 1 л, хліб, гречка\nкуряче філе, заморожені ягоди, мінеральна вода, зубна паста";
    updateInput();
    shopping.focus();
};

function updateProgress() {
    $("progress").textContent =
        `${departments.querySelectorAll("input:checked").length} / ${departments.querySelectorAll("input").length} у кошику`;
}

function addItem(name, item, preview) {
    let section = [...departments.children].find((node) => node.dataset.name === name);
    if (!section) {
        section = document.createElement("section");
        section.dataset.name = name;
        const heading = document.createElement("h3");
        heading.textContent = name;
        section.append(heading, document.createElement("ul"));
        departments.append(section);
    }
    const row = document.createElement("li");
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.disabled = preview;
    checkbox.onchange = updateProgress;
    const title = document.createElement("span");
    title.textContent = item;
    label.append(checkbox, title);
    row.append(label);
    section.lastElementChild.append(row);
    departments.hidden = false;
}

sortForm.onsubmit = async (event) => {
    event.preventDefault();
    if (active) return;
    const text = shopping.value.trim();
    if (!text) {
        shopping.setCustomValidity("Введи хоча б одну покупку.");
        shopping.reportValidity();
        return;
    }
    const config = readSettings();
    if (!config) return;
    busy(true);
    departments.replaceChildren();
    for (const id of ["placeholder", "departments", "progress", "sort-metrics"]) $(id).hidden = true;
    $("output").setAttribute("aria-busy", "true");
    message("status", "Чекаю на перші позиції…");
    let complete = false;
    try {
        await stream("/api/sort", { text, settings: config }, (event) => {
            if (event.type === "item") {
                addItem(event.department, event.text, true);
                message("status", `Розкладаю список… ${departments.querySelectorAll("li").length} позицій`);
            } else if (event.type === "result") {
                departments.replaceChildren();
                for (const group of event.groups) for (const item of group.items) addItem(group.name, item, false);
                $("progress").hidden = false;
                updateProgress();
                const metrics = event.metrics;
                $("sort-metrics").textContent =
                    `TTFT: ${metrics.ttft.toFixed(3)} с · ${metrics.tokens_per_s?.toFixed(1) ?? "—"} ток/с`;
                $("sort-metrics").hidden = false;
                message("status", "Список готовий.");
                complete = true;
            }
        });
        if (!complete) throw new Error("Потік перервано до завершення сортування.");
    } catch (error) {
        departments.hidden = true;
        message("status", error.message, true);
    } finally {
        busy(false);
        $("output").setAttribute("aria-busy", "false");
    }
};

function fillPrompts() {
    $("bench-prompts").value = (benchForm.elements.mode.value === "shopping" ? shoppingPrompts : textPrompts).join(
        "\n",
    );
}
$("bench-examples").onclick = fillPrompts;
benchForm.elements.mode.onchange = () => {
    const current = $("bench-prompts").value;
    if (current === textPrompts.join("\n") || current === shoppingPrompts.join("\n")) fillPrompts();
};

function addRow(row) {
    rows.push(row);
    const line = table.insertRow();
    for (const field of tableColumns) {
        const value = row[field];
        line.insertCell().textContent = field === "reasoning" ? (value ? "Так" : "Ні") : (value ?? "—");
    }
    if (row.last_error) line.title = row.last_error;
    if (row.reasoning) $("reasoning-note").hidden = false;
    $("bench-download").disabled = false;
}

benchForm.onsubmit = async (event) => {
    event.preventDefault();
    if (active) return;
    const config = readSettings();
    if (!config) return;
    const data = Object.fromEntries(new FormData(benchForm));
    data.settings = config;
    data.levels = data.levels
        .trim()
        .split(/[\s,]+/)
        .map(Number);
    data.requests = Number(data.requests);
    data.timeout = Number(data.timeout);
    benchmarkController = new AbortController();
    busy(true);
    $("bench-stop").disabled = false;
    let complete = false;
    message("bench-status", "Підключаюсь до Ollama…");
    try {
        await stream(
            "/api/benchmark",
            data,
            (event) => {
                if (event.type === "warmup") message("bench-status", "Прогріваю модель. Цей запит не враховується.");
                if (event.type === "progress")
                    message("bench-status", `${event.level} клієнтів · ${event.done} із ${event.total} запитів`);
                if (event.type === "row") addRow(event.row);
                if (event.type === "done") complete = true;
            },
            benchmarkController.signal,
        );
        if (!complete) throw new Error("Потік перервано до завершення бенчмарку.");
        message("bench-status", "Бенчмарк завершено.");
    } catch (error) {
        if (benchmarkController.signal.aborted) message("bench-status", "Бенчмарк зупинено.");
        else message("bench-status", error.message, true);
    } finally {
        busy(false);
        $("bench-stop").disabled = true;
        benchmarkController = null;
    }
};
$("bench-stop").onclick = () => benchmarkController?.abort();
$("bench-clear").onclick = () => {
    rows.length = 0;
    table.replaceChildren();
    $("reasoning-note").hidden = true;
    $("bench-download").disabled = true;
    message("bench-status", "");
};
$("bench-download").onclick = () => {
    const escape = (value) => {
        if (typeof value === "boolean") return value ? "True" : "False";
        const safe = typeof value === "string" && /^[=+\-@]/.test(value) ? "'" + value : (value ?? "");
        return '"' + String(safe).replaceAll('"', '""') + '"';
    };
    const csv = [csvColumns.join(","), ...rows.map((row) => csvColumns.map((key) => escape(row[key])).join(","))].join(
        "\r\n",
    );
    const url = URL.createObjectURL(new Blob([csv + "\r\n"], { type: "text/csv;charset=utf-8" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: "bench_results.csv" });
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
};

updateInput();
protocolFields();
fillPrompts();
refreshModels();
