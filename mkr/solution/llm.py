"""Ollama and Groq transport with the same response shape for existing callers."""
import json
import re
from time import sleep, monotonic
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from settings import read_config, provider
from usage_metrics import track_llm


_remaining_tokens = None
_reset_at = 0.0
_quota_model = None


def token_estimate(messages):
    # Base64 is transport encoding, not text tokens. Qwen charges 2048/image.
    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            for part in content:
                total += 2048 if part.get("type") == "image_url" else len(part.get("text", "").encode("utf-8")) // 3
        else:
            total += len(str(content).encode("utf-8")) // 3
    return total


def wait_for_quota(payload, fast=False):
    global _remaining_tokens
    # Conservative estimate; the provider's own headers are authoritative.
    estimate = token_estimate(payload["messages"]) + payload["max_completion_tokens"] + 300
    if _quota_model in (None, payload.get("model")) and _remaining_tokens is not None and estimate > _remaining_tokens:
        remaining = _reset_at - monotonic()
        if fast and remaining > 0:
            raise LLMError("ШІ зайнятий або досягнуто хвилинного ліміту. Спробуй трохи пізніше.")
        if remaining > 65:
            raise LLMError("Ліміт токенів Groq тимчасово вичерпано. Спробуй пізніше.")
        while remaining > 0:
            sleep(min(remaining, 10))
            remaining = _reset_at - monotonic()
        _remaining_tokens = None


def remember_quota(headers, model=None):
    global _remaining_tokens, _reset_at, _quota_model
    _quota_model = model
    try:
        _remaining_tokens = int(headers["x-ratelimit-remaining-tokens"])
        reset = headers.get("x-ratelimit-reset-tokens", "")
        parts = re.findall(r"([\d.]+)(ms|s|m|h)", reset)
        seconds = sum(float(value) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[unit] for value, unit in parts)
        _reset_at = monotonic() + seconds + 1
    except (KeyError, TypeError, ValueError):
        _remaining_tokens = None


class LLMError(ValueError):
    """Safe to show to the user; never includes API bodies, prompts or secrets."""


def error_message(error):
    if isinstance(error, LLMError):
        return str(error)
    return "Не вдалося отримати відповідь ШІ. Перевір налаштування та повтори запит."


def chat(messages, model, *, options, schema=None, think=None, timeout=240, fast=False):
    config = read_config()
    if provider(config) == "groq":
        return groq_chat(messages, model, options, schema, config, timeout, fast=fast)
    return _ollama_chat(messages, model, options=options, schema=schema, think=think, timeout=timeout)


@track_llm
def _ollama_chat(messages, model, *, options, schema=None, think=None, timeout=240):
    payload = {"model": model, "stream": False, "messages": messages, "options": options}
    if schema is not None:
        payload["format"] = schema
    if think is not None:
        payload["think"] = think
    request = Request("http://127.0.0.1:11434/api/chat", data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


@track_llm
def groq_chat(messages, model, options, schema, config, timeout, fast=False):
    key = config.get("GROQ_API_KEY", "").strip()
    if not key:
        raise LLMError("Не задано GROQ_API_KEY у конфігурації бота.")
    payload = {"model": model, "messages": messages, "stream": False,
               "temperature": options.get("temperature", 0),
               "max_completion_tokens": max(2048, options.get("num_predict", 1000))}
    if model.startswith("openai/gpt-oss-"):
        payload["reasoning_effort"] = "low"
        payload["include_reasoning"] = False
    elif model == "qwen/qwen3.8-27b":
        payload["reasoning_effort"] = "none"
    if schema is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "news_result", "strict": True, "schema": schema}}
    request = Request("https://api.groq.com/openai/v1/chat/completions",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                               "User-Agent": "PersonalNewsBot/1.0"})
    wait_for_quota(payload, fast=fast)
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                remember_quota(response.headers, model)
                result = json.load(response)
            choice = result["choices"][0]
            content = choice["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise LLMError("Groq повернув порожню відповідь. Повтори запит.")
            return {"message": {"role": "assistant", "content": content},
                    "done_reason": choice.get("finish_reason"), "usage": result.get("usage", {})}
        except HTTPError as exc:
            code = exc.code
            try:
                delay = float(exc.headers.get("Retry-After", 2 ** attempt))
            except (ValueError, TypeError):
                delay = 2 ** attempt
            exc.close()
            if not fast and code in {429, 500, 502, 503, 504} and attempt < 2 and 0 <= delay <= 60:
                while delay > 0:
                    step = min(delay, 10)
                    sleep(step)
                    delay -= step
                continue
            if code == 429:
                raise LLMError("Досягнуто ліміту Groq. Спробуй пізніше; денний ліміт відновиться за правилами Groq.") from None
            if code in {401, 403}:
                raise LLMError("Groq відхилив доступ. Перевір API-ключ і дозволи моделі.") from None
            if code in {400, 404, 413, 422}:
                raise LLMError("Groq не прийняв модель, формат або розмір запиту. Перевір назву моделі та налаштування.") from None
            raise LLMError("Groq тимчасово недоступний. Спробуй пізніше.") from None
        except (KeyError, IndexError, TypeError):
            raise LLMError("Groq повернув невідому структуру відповіді.") from None
