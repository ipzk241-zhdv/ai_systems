import json
import os
import re
import secrets
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from textwrap import wrap
from time import sleep
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from digest import MODEL, ollama_chat, select_items
from html_digest import fetch_market, render_html
from outages import current_message, schedule_messages
from sources import KYIV, collect, page


SINCE_LAST = "З останньої перевірки"
OUTAGE_BUTTON = "Відключення"
WINDOWS = {"За 4 години": 4, "За 8 годин": 8, "За 24 години": 24, SINCE_LAST: 24}
BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".runtime"
CHAT_MODEL = "qwen3.5:9b"
CHAT_PROMPT = ("Відповідай прямо й стисло. Українська — мова за замовчуванням. "
               "Якщо не впевнений у відповіді, скажи про це.")
DEFINITION_QUERY = re.compile(
    r'''(?P<prefix>що\s+(?:означає|значить)\s+(?:слово\s+)?|значення\s+слова\s+)?'''
    r'''[«"„“]?(?P<word>[а-щьюяєіїґ][а-щьюяєіїґ’'ʼ-]{0,38})[»"“”]?[?!.]?''',
    re.IGNORECASE,
)
CHAT_REPLIES = {"так", "ні", "привіт", "вітаю", "дякую", "добре", "гаразд", "ок", "окей",
                "чому", "як", "коли", "де", "що", "далі", "коротше", "детальніше", "продовжуй", "повтори"}
CHAT_MARKUP = re.compile(r"(```[^`\n]*\n[\s\S]*?```|`[^`\n]+`|(?<!\*)\*\*(?=\S)[^*\n]+?(?<=\S)\*\*(?!\*))")
KEYBOARD = {
    "keyboard": [[{"text": label} for label in row] for row in
                 (list(WINDOWS)[:2], ["За 24 години"], [SINCE_LAST], [OUTAGE_BUTTON])],
    "resize_keyboard": True,
    "is_persistent": True,
}


class TelegramError(Exception):
    def __init__(self, code=None, retry_after=5):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API: HTTP {code}" if code else "Telegram API недоступний.")


class Telegram:
    def __init__(self, token):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        return self.request(method, json.dumps(params).encode("utf-8"), "application/json",
                            timeout=params.get("timeout", 0) + 15)

    def request(self, method, body, content_type, timeout):
        request = Request(self.base_url + method, data=body, headers={"Content-Type": content_type})
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                retry_after = json.load(exc).get("parameters", {}).get("retry_after", 5)
            except (OSError, ValueError):
                retry_after = 5
            finally:
                exc.close()
            raise TelegramError(exc.code, retry_after) from None
        except (OSError, ValueError):
            raise TelegramError() from None
        if not result.get("ok"):
            raise TelegramError(result.get("error_code"))
        return result["result"]

    def send(self, chat_id, text, **params):
        self.call("sendMessage", chat_id=chat_id, text=text, **params)

    def send_document(self, chat_id, filename, html):
        boundary = secrets.token_hex(24)
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                'Content-Type: text/html; charset=utf-8\r\n\r\n')
        body = f"{head}{html}\r\n--{boundary}--\r\n".encode("utf-8")
        self.request("sendDocument", body, f"multipart/form-data; boundary={boundary}", timeout=45)


def open_state(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS checks (
        chat_id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, update_id INTEGER NOT NULL
    )""")
    return db


def read_config():
    values = {}
    try:
        for line in (BASE_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in {"TELEGRAM_BOT_TOKEN", "OLLAMA_MODEL"}:
                values[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return {**values, **os.environ}


def window_start(button, last_check, now):
    earliest = now - timedelta(hours=WINDOWS[button])
    if button == SINCE_LAST and last_check is not None:
        return max(earliest, min(last_check, now))
    return earliest


def definition_word(text):
    match = DEFINITION_QUERY.fullmatch(text)
    if not match:
        return None
    word = match["word"].lower()
    if match["prefix"] or word not in CHAT_REPLIES:
        return word
    return None


def lookup_definition(word):
    url = f"https://slovnyk.me/dict/sum/{quote(word)}"
    try:
        soup = page(url)
    except (OSError, ValueError):
        return None
    entry = soup.select_one("article h1") or soup.find("h1")
    description = soup.select_one('meta[name="description"][content]')
    if not entry or entry.get_text(strip=True).lower() != word or not description:
        return None
    excerpt = description["content"].split(" — ", 1)[0].split()
    if not excerpt:
        return None
    short = " ".join(excerpt[:22]) + ("…" if len(excerpt) > 22 else "")
    return f"{short}\n\nДжерело: СУМ-11\n{url}"


def format_chat_html(text):
    parts = CHAT_MARKUP.split(text)
    for index, part in enumerate(parts):
        if index % 2 == 0:
            parts[index] = escape(part, quote=False)
        elif part.startswith("```"):
            code = part.partition("\n")[2][:-3].rstrip("\n")
            parts[index] = f"<pre>{escape(code, quote=False)}</pre>"
        elif part.startswith("`"):
            parts[index] = f"<code>{escape(part[1:-1], quote=False)}</code>"
        else:
            parts[index] = f"<b>{escape(part[2:-2], quote=False)}</b>"
    return "".join(parts)


def answer_question(api, chat_id, text, conversations):
    if len(text) > 1500:
        api.send(chat_id, "Скороти запитання до 1500 символів і надішли ще раз.")
        return

    word = definition_word(text)
    previous = [] if word else conversations.get(chat_id, [])
    history = [*previous, {"role": "user", "content": text}]
    api.call("sendChatAction", chat_id=chat_id, action="typing")
    try:
        answer = lookup_definition(word) if word else None
        if answer is None:
            messages = [{"role": "system", "content": CHAT_PROMPT}, *history]
            if word:
                messages[-1] = {"role": "user", "content": f"Коротко поясни значення слова «{word}»."}
            result = ollama_chat(messages, CHAT_MODEL,
                                 options={"temperature": 0.35, "num_ctx": 8192, "num_predict": 650},
                                 think=False, timeout=120)
            answer = result["message"]["content"].strip()
        if not answer:
            raise ValueError("Порожня відповідь моделі.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"Не вдалося отримати відповідь Qwen ({type(exc).__name__}).", file=sys.stderr)
        api.send(chat_id, "Qwen зараз недоступна. Спробуй ще раз трохи пізніше.")
        return

    chunks = [answer] if len(answer.encode("utf-16-le")) <= 8000 else wrap(answer, 1900, replace_whitespace=False)
    for chunk in chunks:
        api.send(chat_id, format_chat_html(chunk), parse_mode="HTML", link_preview_options={"is_disabled": True})
    conversations[chat_id] = [*history, {"role": "assistant", "content": answer}][-6:]
    conversations.move_to_end(chat_id)
    if len(conversations) > 32:
        conversations.popitem(last=False)


def handle_update(api, update, db, conversations, model):
    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    update_id = update["update_id"]
    if chat.get("type") != "private" or chat_id is None:
        return
    text = message.get("text", "").strip()
    if text == "/start" or text.startswith("/start "):
        api.send(chat_id, "Обери період для дайджесту, натисни «Відключення» або напиши запитання Qwen.",
                 reply_markup=KEYBOARD)
        return
    action, _, street = text.partition(":")
    if action.strip().casefold() == OUTAGE_BUTTON.casefold():
        street = street.strip()
        if len(street) > 80:
            api.send(chat_id, "Вкажи тільки назву вулиці, до 80 символів.")
            return
        api.call("sendChatAction", chat_id=chat_id, action="typing")
        if not street:
            for report in schedule_messages(datetime.now(KYIV).date()):
                api.send(chat_id, report, parse_mode="HTML", link_preview_options={"is_disabled": True})
        report = current_message(datetime.now(KYIV), street or None)
        api.send(chat_id, report, parse_mode="HTML", link_preview_options={"is_disabled": True})
        return
    if text not in WINDOWS:
        if text and not text.startswith("/"):
            answer_question(api, chat_id, text, conversations)
        return

    previous = db.execute("SELECT checked_at, update_id FROM checks WHERE chat_id = ?",
                          (chat_id,)).fetchone()
    if previous and previous[1] == update_id:
        return
    until = datetime.now(timezone.utc)
    since = window_start(text, datetime.fromisoformat(previous[0]) if previous else None, until)
    api.send(chat_id, "Збираю новини…")
    try:
        articles, status = collect(since, until)
        if not articles and all(error for _, error in status.values()):
            api.send(chat_id, "Джерела зараз недоступні. Спробуй ще раз трохи пізніше.")
            return
        items = select_items(articles, model)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"Не вдалося зібрати дайджест ({type(exc).__name__}).", file=sys.stderr)
        api.send(chat_id, "Дайджест не сформувався. Перевір, чи працює Ollama, і повтори запит.")
        return

    html = render_html(articles, items, status, fetch_market(), since, until)
    filename = f"digest-{until.astimezone(KYIV):%Y-%m-%d-%H%M}.html"
    api.send_document(chat_id, filename, html)
    with db:
        db.execute("""INSERT INTO checks (chat_id, checked_at, update_id) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET checked_at=excluded.checked_at, update_id=excluded.update_id
        """, (chat_id, until.isoformat(), update_id))


def poll(api, db, model):
    offset = 0
    conversations = OrderedDict()
    while True:
        try:
            updates = api.call("getUpdates", timeout=25, offset=offset, limit=1,
                               allowed_updates=["message"])
        except TelegramError as exc:
            if exc.code in {401, 404, 409}:
                raise
            print(exc, file=sys.stderr)
            sleep(max(1, exc.retry_after))
            continue
        for update in updates:
            try:
                handle_update(api, update, db, conversations, model)
            except TelegramError as exc:
                if exc.code in {401, 404}:
                    raise
                print(exc, file=sys.stderr)
            offset = update["update_id"] + 1


def main():
    try:
        config = read_config()
        token = config.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            print("Вкажи TELEGRAM_BOT_TOKEN у файлі .env поруч зі скриптом або у змінній середовища.",
                  file=sys.stderr)
            return 1
        api = Telegram(token)
        bot = api.call("getMe")
        RUNTIME_DIR.mkdir(exist_ok=True)
        db = open_state(RUNTIME_DIR / f"bot-{bot['id']}.sqlite3")
        try:
            print(f"Бот запущено: https://t.me/{bot['username']}", flush=True)
            poll(api, db, config.get("OLLAMA_MODEL") or MODEL)
        finally:
            db.close()
    except TelegramError as exc:
        print(exc, file=sys.stderr)
        if exc.code == 409:
            print("Перевір, чи бот уже запущений в іншому процесі або має webhook.", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f"Не вдалося запустити бота або зберегти стан ({type(exc).__name__}).", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Бот зупинено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
