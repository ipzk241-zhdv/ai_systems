import json
import re
import secrets
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from textwrap import wrap
from time import sleep, monotonic
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from digest import MODEL, ollama_chat, select_items, limit_items
from html_digest import fetch_market, render_html
from news_history import initialize, save_articles, previous_articles, compare_items, prepare_digest, confirm_delivery
from news_questions import respond as news_response, EXPLAIN, COMPARE
from outages import current_message, schedule_messages
from sources import KYIV, collect, page
from settings import read_config, default_model, provider
from llm import error_message
from inline_bot import InlineAnswers
from message_worker import PrivateMessages
from guest_bot import GuestAnswers
from voice_notes import initialize as initialize_voice, handle_voice, voice_source, VoiceError


SINCE_LAST = "З останньої перевірки"
OUTAGE_BUTTON = "Відключення"
WINDOWS = {"За 4 години": 4, "За 8 годин": 8, "За 24 години": 24, SINCE_LAST: 24}
BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".runtime"
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
                 (list(WINDOWS)[:2], ["За 24 години"], [SINCE_LAST], ["Що змінилося", OUTAGE_BUTTON])],
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
        self.file_base_url = f"https://api.telegram.org/file/bot{token}/"

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

    def download_voice(self, file_id, max_bytes):
        file = self.call("getFile", file_id=file_id)
        path = file.get("file_path", "")
        if (not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/")
                or "\\" in path or ":" in path):
            raise VoiceError("Telegram не повернув коректну адресу аудіо.")
        if file.get("file_size", 0) > max_bytes:
            raise VoiceError("Голосове перевищує обмеження 19 МБ.")
        extension = Path(path).suffix.lower()
        if extension == ".oga":
            extension = ".ogg"
        if extension not in {".ogg", ".mp3", ".m4a", ".wav", ".webm", ".mp4"}:
            raise VoiceError("Непідтримуваний формат аудіо.")
        try:
            with urlopen(self.file_base_url + quote(path, safe="/"), timeout=45) as response:
                data = response.read(max_bytes + 1)
        except OSError:
            raise VoiceError("Не вдалося завантажити голосове з Telegram.") from None
        if not data or len(data) > max_bytes:
            raise VoiceError("Аудіо порожнє або перевищує обмеження 19 МБ.")
        return data, "voice" + extension

    def send_document(self, chat_id, filename, html):
        boundary = secrets.token_hex(24)
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                'Content-Type: text/html; charset=utf-8\r\n\r\n')
        body = f"{head}{html}\r\n--{boundary}--\r\n".encode("utf-8")
        self.request("sendDocument", body, f"multipart/form-data; boundary={boundary}", timeout=45)


def open_state(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("""CREATE TABLE IF NOT EXISTS checks (
        chat_id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, update_id INTEGER NOT NULL
    )""")
    initialize(db)
    initialize_voice(db)
    return db


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


def answer_question(api, chat_id, text, conversations, model=MODEL):
    if len(text) > 1500:
        api.send(chat_id, "Скороти запитання до 1500 символів і надішли ще раз.")
        return

    word = definition_word(text)
    previous = [] if word else conversations.get(chat_id, [])
    if provider(read_config()) == "groq":
        while previous and sum(len(entry["content"]) for entry in previous) > 4500:
            previous = previous[2:]
    history = [*previous, {"role": "user", "content": text}]
    api.call("sendChatAction", chat_id=chat_id, action="typing")
    try:
        answer = lookup_definition(word) if word else None
        if answer is None:
            messages = [{"role": "system", "content": CHAT_PROMPT}, *history]
            if word:
                messages[-1] = {"role": "user", "content": f"Коротко поясни значення слова «{word}»."}
            result = ollama_chat(messages, model,
                                 options={"temperature": 0.35, "num_ctx": 8192, "num_predict": 650},
                                 think=False, timeout=120)
            answer = result["message"]["content"].strip()
        if not answer:
            raise ValueError("Порожня відповідь моделі.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"Не вдалося отримати відповідь ШІ ({type(exc).__name__}).", file=sys.stderr)
        api.send(chat_id, error_message(exc))
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
    if voice_source(message):
        handle_voice(api, chat_id, message, db, conversations)
        return
    text = message.get("text", "").strip()
    if text == "/start" or text.startswith("/start "):
        api.send(chat_id, "Обери період для дайджесту, натисни «Відключення» або напиши запитання ШІ.\n"
                 "«Поясни 3» — пояснення новини №3 з останнього дайджесту.\n"
                 "«Поясни 3: що це означає для мешканців?» — уточнення до статті.\n"
                 "«Що змінилося» — порівняння двох останніх надісланих випусків.\n"
                 "Надішли голосове або відповідай на нього зі згадкою @DBresh_bot — отримаєш розшифровку.\n"
                 "У звичайному приватному чаті чи групі: надішли @DBresh_bot запитання — бот відповість сам (Guest Mode).",
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
        if EXPLAIN.fullmatch(text) or text.casefold().rstrip(".!?") in COMPARE:
            api.call("sendChatAction", chat_id=chat_id, action="typing")
            try:
                answer = news_response(db, chat_id, text, model)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
                print(f"Не вдалося пояснити новини ({type(exc).__name__}).", file=sys.stderr)
                api.send(chat_id, error_message(exc))
                return
            for chunk in wrap(answer, 1900, replace_whitespace=False, break_long_words=False, break_on_hyphens=False):
                api.send(chat_id, format_chat_html(chunk), parse_mode="HTML", link_preview_options={"is_disabled": True})
            return
        if text and not text.startswith("/"):
            answer_question(api, chat_id, text, conversations, model)
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
        article_ids = save_articles(db, articles, until)
        items = select_items(articles, model, limit=None)
        items, updates, repeated = compare_items(articles, items, previous_articles(db, chat_id, until), model)
        items = limit_items(articles, items)
        shown_urls = {articles[item["id"] - 1].url for item in items}
        updates = {url: previous for url, previous in updates.items() if url in shown_urls}
        html = render_html(articles, items, status, fetch_market(), since, until,
                           updates=updates, repeated=repeated)
        digest_id = prepare_digest(db, chat_id, since, until, html, articles, article_ids, items, updates)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
        print(f"Не вдалося зібрати дайджест ({type(exc).__name__}).", file=sys.stderr)
        api.send(chat_id, "Дайджест не сформувався. " + error_message(exc))
        return

    filename = f"digest-{until.astimezone(KYIV):%Y-%m-%d-%H%M}.html"
    api.send_document(chat_id, filename, html)
    confirm_delivery(db, digest_id, chat_id, until, update_id)


def poll(api, db, model):
    offset = 0
    database_path = db.execute("PRAGMA database_list").fetchone()[2]
    messages = PrivateMessages(api, database_path, model, handle_update, open_state)
    inline = InlineAnswers(api, model, format_chat_html)
    guests = GuestAnswers(api, model, format_chat_html, username=getattr(api, "username", "DBresh_bot"), database_path=database_path)
    messages.start()
    inline.start()
    guests.start()
    try:
        while True:
            messages.check()
            try:
                updates = api.call("getUpdates", timeout=25, offset=offset, limit=20,
                                   allowed_updates=["message", "inline_query", "guest_message"])
            except TelegramError as exc:
                if exc.code in {401, 404, 409}:
                    raise
                print(exc, file=sys.stderr)
                sleep(max(1, exc.retry_after))
                continue
            for update in updates:
                try:
                    if "guest_message" in update:
                        update["guest_message"]["_bot_received_at"] = monotonic()
                        guests.submit(update["guest_message"])
                    elif "inline_query" in update:
                        inline.submit(update["inline_query"])
                    elif "message" in update:
                        update["message"]["_bot_received_at"] = monotonic()
                        if guests.accepts(update["message"]):
                            guests.submit(update["message"])
                        else:
                            messages.submit(update)
                except TelegramError as exc:
                    if exc.code in {401, 404}:
                        raise
                    print(exc, file=sys.stderr)
                offset = update["update_id"] + 1
    finally:
        inline.close()
        guests.close()
        messages.close()


def main():
    try:
        config = read_config()
        model = default_model(config)
        if provider(config) == "groq" and not config.get("GROQ_API_KEY", "").strip():
            print("Вкажи GROQ_API_KEY у solution/.env.", file=sys.stderr)
            return 1
        token = config.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            print("Вкажи TELEGRAM_BOT_TOKEN у файлі .env поруч зі скриптом або у змінній середовища.",
                  file=sys.stderr)
            return 1
        api = Telegram(token)
        bot = api.call("getMe")
        api.username = bot["username"]
        if not bot.get("supports_inline_queries"):
            print("Inline mode: enable /setinline in @BotFather for queries in other chats.", flush=True)
        RUNTIME_DIR.mkdir(exist_ok=True)
        db = open_state(RUNTIME_DIR / f"bot-{bot['id']}.sqlite3")
        try:
            print(f"Бот запущено: https://t.me/{bot['username']}", flush=True)
            print(f"ШІ: {provider(config)} · {model}", flush=True)
            print("Для зупинки натисни Ctrl+C або закрий вікно консолі.", flush=True)
            poll(api, db, model)
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
