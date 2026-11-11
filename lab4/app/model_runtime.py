import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OLLAMA_URL = "http://127.0.0.1:11434"


# Помилка Ollama або відповіді моделі, яку можна показати в інтерфейсі.
class ModelError(Exception):
    pass


# Виконує запит до Ollama і читає готову JSON-відповідь.
def request_ollama(path, payload=None, timeout=45):
    try:
        with connect(path, payload, timeout) as response:
            return json.load(response)
    except (TimeoutError, OSError, ValueError) as error:
        raise stream_error(error) from error


# Відкриває HTTP-з’єднання й за потреби передає JSON у тілі запиту.
def connect(path, payload, timeout):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    return urlopen(Request(OLLAMA_URL + path, data=body, headers={"Content-Type": "application/json"}), timeout=timeout)


# Перетворює помилки мережі, HTTP і розбору даних на повідомлення для користувача.
def stream_error(error):
    if isinstance(error, HTTPError):
        if error.code == 404:
            return ModelError("Модель відсутня в Ollama. Перевір назву й список встановлених моделей.")
        try:
            message = json.load(error).get("error", "")
            message = message.get("message", "") if isinstance(message, dict) else message
        except (OSError, ValueError, AttributeError):
            message = ""
        return ModelError(f"Ollama повернула HTTP {error.code}" + (f". {str(message)[:300]}" if message else "."))
    if isinstance(error, TimeoutError) or (isinstance(error, URLError) and isinstance(error.reason, TimeoutError)):
        return ModelError("Ollama не встигла відповісти. Збільш таймаут або скороти запит.")
    if isinstance(error, URLError):
        return ModelError("Сервер Ollama недоступний. Перевір, чи він запущений.")
    return ModelError("Ollama повернула некоректний або перерваний потік.")


# Перевіряє модель і параметри генерації, які надійшли з форми.
def read_settings(data, installed):
    if not isinstance(data, dict):
        raise ValueError("Некоректні налаштування моделі.")
    model = data.get("model")
    if model not in installed:
        raise ValueError("Вибери одну зі встановлених локальних моделей.")
    protocol = data.get("protocol")
    if protocol not in ("openai", "ollama"):
        raise ValueError("Невідомий протокол.")
    reasoning = data.get("reasoning")
    if reasoning not in ("", "none", "low", "medium", "high"):
        raise ValueError("Невідомий режим міркування.")
    options = {}
    # Для кожного параметра задаємо допустимі межі та числовий тип.
    ranges = {
        "max_tokens": (1, 4096, int),
        "temperature": (0, 2, float),
        "top_p": (0, 1, float),
        "seed": (-2147483648, 2147483647, int),
        "frequency_penalty": (-2, 2, float),
        "presence_penalty": (-2, 2, float),
    }
    if protocol == "ollama":
        ranges.update(top_k=(1, 200, int), num_ctx=(256, 32768, int))
    for key, (lower, upper, kind) in ranges.items():
        value = data.get(key)
        # Порожні поля пропускаємо, щоб Ollama використала свої значення за замовчуванням.
        if value is None:
            continue
        if type(value) not in (int, float) or not lower <= value <= upper or (kind is int and int(value) != value):
            raise ValueError(f"{key}: введи значення від {lower} до {upper}.")
        options[key] = kind(value)
    if "max_tokens" not in options:
        raise ValueError("Вкажи ліміт токенів відповіді.")
    return model, protocol, reasoning, options


# Читає потік Ollama й видає текст, міркування та лічильник токенів.
def stream_chunks(settings, messages, schema, timeout):
    model, protocol, reasoning, options = settings
    # Два API використовують різні назви полів для схеми, міркування й ліміту токенів.
    if protocol == "openai":
        path = "/v1/chat/completions"
        payload = dict(model=model, messages=messages, stream=True, stream_options={"include_usage": True}, **options)
        if schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "departments", "schema": schema}}
        if reasoning:
            payload["reasoning_effort"] = reasoning
    else:
        path = "/api/chat"
        native = {key: value for key, value in options.items() if key != "max_tokens"}
        native["num_predict"] = options["max_tokens"]
        payload = dict(model=model, messages=messages, stream=True, options=native)
        if schema:
            payload["format"] = schema
        if reasoning:
            payload["think"] = reasoning != "none"

    started = time.perf_counter()
    try:
        with connect(path, payload, timeout) as response:
            for line in response:
                if time.perf_counter() - started > timeout:
                    raise TimeoutError()
                line = line.strip()
                if not line:
                    continue
                # OpenAI-сумісний потік має префікс data: і маркер [DONE].
                if protocol == "openai":
                    if not line.startswith(b"data:"):
                        continue
                    line = line[5:].strip()
                    if line == b"[DONE]":
                        return
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise ModelError(str(chunk["error"])[:300])
                # Зводимо обидва формати до однакового набору значень для вимірювання.
                if protocol == "openai":
                    choices = chunk.get("choices") or []
                    delta = choices[0].get("delta", {}) if choices else {}
                    tokens = (chunk.get("usage") or {}).get("completion_tokens")
                    yield delta.get("content"), delta.get("reasoning") or delta.get("reasoning_content"), tokens
                else:
                    message = chunk.get("message") or {}
                    yield message.get("content"), message.get("thinking"), chunk.get("eval_count")
                    if chunk.get("done"):
                        return
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise stream_error(error) from error
    raise ModelError("Потік Ollama обірвався до завершення.")


# Вимірює один запит і передає видимий текст обробнику, якщо його задано.
def measure(settings, messages, schema, timeout, on_text=None, cancelled=None):
    started = time.perf_counter()
    first = tokens = None
    reasoning = False
    chunks = stream_chunks(settings, messages, schema, timeout)
    try:
        for content, thought, usage in chunks:
            if cancelled and cancelled.is_set():
                raise ModelError("Бенчмарк зупинено.")
            # TTFT рахуємо до першого видимого фрагмента відповіді.
            if content:
                if first is None:
                    first = time.perf_counter()
                if on_text:
                    on_text(content)
            if usage is not None:
                tokens = usage
            reasoning = reasoning or bool(thought)
    finally:
        chunks.close()
    end = time.perf_counter()
    if first is None:
        raise ModelError("Модель не надіслала текст. Спробуй вимкнути міркування або збільшити ліміт токенів.")
    # Швидкість після першого токена рахуємо за рештою токенів.
    return {
        "ttft": first - started,
        "tokens": tokens,
        "tokens_per_s": (tokens - 1) / (end - first) if tokens and end > first else None,
        "reasoning": reasoning,
    }


# Обчислює перцентиль через лінійну інтерполяцію між сусідніми вимірами.
def percentile(values, q, digits):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), digits)


# Проганяє один рівень паралельності й повертає зведені метрики.
def run_level(settings, jobs, level, count, timeout, label, notify, cancelled):
    started = time.perf_counter()
    pool = ThreadPoolExecutor(max_workers=level)
    results = []
    errors = 0
    last_error = ""
    try:
        # Якщо запитів більше за кількість промптів, беремо промпти по колу.
        futures = [pool.submit(measure, settings, *jobs[i % len(jobs)], timeout, cancelled=cancelled) for i in range(count)]
        for index, future in enumerate(as_completed(futures), 1):
            try:
                results.append(future.result())
            except ModelError as error:
                errors += 1
                last_error = str(error)
            notify({"type": "progress", "level": level, "done": index, "total": count})
    # Під час завершення подаємо сигнал активним запитам і скасовуємо ті, що чекають.
    finally:
        cancelled.set()
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False)
        
    # Throughput ділить суму токенів усіх запитів на час усього прогону.
    wall = time.perf_counter() - started
    tokens = [r["tokens"] for r in results if r["tokens"] is not None]
    times = [r["ttft"] for r in results]
    speeds = [r["tokens_per_s"] for r in results if r["tokens_per_s"]]
    return {
        "label": label, "model": settings[0], "concurrency": level, "requests": count, "errors": errors,
        "ttft_p50_s": percentile(times, 50, 3), "ttft_p95_s": percentile(times, 95, 3),
        "ttft_p99_s": percentile(times, 99, 3),
        "tokens_per_s_per_user_p50": percentile(speeds, 50, 1),
        "tokens_per_s_per_user_p95": percentile(speeds, 95, 1),
        "tokens_per_s_per_user_p99": percentile(speeds, 99, 1),
        "throughput_tokens_per_s": round(sum(tokens) / wall, 1) if tokens else None,
        "reasoning": any(r["reasoning"] for r in results),
        "duration_s": round(wall, 2), "tokens_per_request": round(sum(tokens) / len(tokens), 1) if tokens else None,
        "last_error": last_error,
    }
