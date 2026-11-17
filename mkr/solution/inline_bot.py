"""Short, stateless inline answers, separate from private-chat history."""
from collections import OrderedDict
import hashlib
import sys
from threading import Condition, Thread
from time import monotonic

from llm import chat


INLINE_PROMPT = ("Відповідай прямо й стисло, до 120 слів. Українська — мова за замовчуванням; "
                 "якщо запит іншою мовою, відповідай нею. Якщо не знаєш, скажи про це. "
                 "Ти отримуєш лише цей запит, без історії чату. Не стверджуй, що бачиш "
                 "повідомлення співрозмовників, маєш актуальні новини чи виконував дії, яких не виконував.")


def short_answer(prompt, model, timeout):
    result = chat([{"role": "system", "content": INLINE_PROMPT},
                   {"role": "user", "content": prompt}], model,
                  options={"temperature": .3, "num_predict": 650}, think=False,
                  timeout=timeout, fast=True)
    answer = result["message"]["content"].strip()
    if not answer or result.get("done_reason") == "length":
        raise ValueError("Incomplete inline answer")
    return answer


def trim_text(text, units=3400):
    raw = text.encode("utf-16-le")
    if len(raw) <= units * 2:
        return text
    return raw[:(units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


class InlineAnswers:
    def __init__(self, api, model, formatter, generate=short_answer, debounce=1.0):
        self.api, self.model, self.formatter = api, model, formatter
        self.generate, self.debounce = generate, debounce
        self.pending, self.cache = OrderedDict(), OrderedDict()
        self.latest = OrderedDict()
        self.condition = Condition()
        self.stopped = False
        self.thread = Thread(target=self.run, name="inline-answers", daemon=True)

    def start(self):
        self.thread.start()

    def hint(self, query_id, text):
        self.api.call("answerInlineQuery", inline_query_id=query_id, results=[],
                      cache_time=0, is_personal=True,
                      button={"text": text, "start_parameter": "inline"})

    def answer(self, query_id, prompt, answer):
        answer = trim_text(answer)
        result_id = hashlib.sha256((prompt + "\n" + answer).encode("utf-8")).hexdigest()[:32]
        result = {"type": "article", "id": result_id, "title": "Відповідь ШІ · натисни, щоб надіслати",
                  "description": " ".join(answer.split())[:150], "input_message_content": {
                      "message_text": self.formatter(answer), "parse_mode": "HTML",
                      "link_preview_options": {"is_disabled": True}}}
        self.api.call("answerInlineQuery", inline_query_id=query_id, results=[result],
                      cache_time=120, is_personal=True)

    def submit(self, query):
        query_id, user = query.get("id"), query.get("from", {}).get("id")
        if not query_id or type(user) is not int:
            return
        prompt = query.get("query", "").strip()
        now = monotonic()
        with self.condition:
            self.latest[user] = query_id
            self.latest.move_to_end(user)
            while len(self.latest) > 128:
                self.latest.popitem(last=False)
            self.pending.pop(user, None)
            cached = self.cache.get((user, prompt))
            if cached and now - cached[0] < 300:
                self.cache.move_to_end((user, prompt))
            else:
                cached = None
            if len(prompt) >= 3 and len(prompt) <= 400 and not query.get("offset") and not cached:
                self.pending[user] = (query_id, prompt, now)
                while len(self.pending) > 32:
                    self.pending.popitem(last=False)
                self.condition.notify()
                return
            self.condition.notify()
        if cached:
            self.answer(query_id, prompt, cached[1])
        elif len(prompt) > 400:
            self.hint(query_id, "Скороти запит до 400 символів або відкрий бота")
        else:
            self.hint(query_id, "Напиши запитання або відкрий приватний чат")

    def run(self):
        while True:
            with self.condition:
                while not self.stopped and not self.pending:
                    self.condition.wait()
                if self.stopped:
                    return
                user, (query_id, prompt, received) = next(iter(self.pending.items()))
                wait = received + self.debounce - monotonic()
                if wait > 0:
                    self.condition.wait(wait)
                    continue
                self.pending.pop(user)
            try:
                timeout = min(5.0, 8.0 - (monotonic() - received) - 1.0)
                if timeout < 1:
                    self.hint(query_id, "ШІ зайнятий — спробуй ще раз або відкрий бота")
                    continue
                answer = self.generate(prompt, self.model, timeout)
                with self.condition:
                    self.cache[(user, prompt)] = (monotonic(), answer)
                    self.cache.move_to_end((user, prompt))
                    while len(self.cache) > 128:
                        self.cache.popitem(last=False)
                    current = self.latest.get(user) == query_id and not self.stopped
                if current:
                    self.answer(query_id, prompt, answer)
            except Exception as error:
                # Telegram may reject a query that expired while the user was typing.
                print("Inline answer unavailable (" + type(error).__name__ + ").", file=sys.stderr)
                with self.condition:
                    current = self.latest.get(user) == query_id and not self.stopped
                if current:
                    try:
                        self.hint(query_id, "Відповідь недоступна — спробуй ще раз або відкрий бота")
                    except Exception:
                        pass

    def close(self):
        with self.condition:
            self.stopped = True
            self.pending.clear()
            self.condition.notify_all()
        self.thread.join(timeout=1)
