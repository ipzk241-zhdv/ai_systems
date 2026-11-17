"""Voice-to-text through Groq, with an explicitly estimated time saving."""
from datetime import datetime, timezone
from html import escape
import json
import math
import re
import secrets
import sys
from time import monotonic
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from settings import read_config


MAX_BYTES = 19_000_000
MAX_DURATION = 600
STT_MODELS = {"whisper-large-v3-turbo", "whisper-large-v3"}
WORDS = re.compile(r"[^\W_]+(?:[’'ʼ-][^\W_]+)*", re.UNICODE)


class VoiceError(ValueError):
    """Safe to show; does not contain HTTP URLs, tokens, audio or transcripts."""


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS voice_runs (
        id INTEGER PRIMARY KEY,
        completed_at TEXT NOT NULL,
        audio_seconds REAL NOT NULL,
        word_count INTEGER NOT NULL,
        processing_seconds REAL NOT NULL,
        reading_wpm REAL NOT NULL,
        playback_speed REAL NOT NULL,
        estimated_saved_seconds REAL NOT NULL,
        stt_model TEXT NOT NULL
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(voice_runs)")}
    if "context" not in columns:
        db.execute("ALTER TABLE voice_runs ADD COLUMN context TEXT NOT NULL DEFAULT 'private'")
    db.commit()


def positive(value, name, maximum):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise VoiceError("Перевір параметр " + name + " у .env.") from None
    if not math.isfinite(result) or not 0 < result <= maximum:
        raise VoiceError("Перевір параметр " + name + " у .env.")
    return result


def preferences(config):
    return (positive(config.get("READING_WPM", "200"), "READING_WPM", 2000),
            positive(config.get("PLAYBACK_SPEED", "1"), "PLAYBACK_SPEED", 4))


def estimate(duration, words, processing, reading_wpm=200, playback_speed=1):
    if not all(math.isfinite(v) and v >= 0 for v in (duration, words, processing)):
        raise ValueError("Invalid timing values")
    reading_wpm = positive(reading_wpm, "READING_WPM", 2000)
    playback_speed = positive(playback_speed, "PLAYBACK_SPEED", 4)
    listening = duration / playback_speed
    reading = 60 * words / reading_wpm
    return {"listening_seconds": listening, "reading_seconds": reading,
            "total_seconds": processing + reading, "saved_seconds": listening - processing - reading}


def transcribe(audio, filename="voice.ogg", config=None):
    config = read_config() if config is None else config
    key = config.get("GROQ_API_KEY", "").strip()
    if not key:
        raise VoiceError("Для голосових додай GROQ_API_KEY у .env. Розпізнавання працює через Groq, окремо від текстової моделі.")
    if not audio or len(audio) > MAX_BYTES:
        raise VoiceError("Аудіо порожнє або перевищує 19 МБ.")
    model = config.get("STT_MODEL", "whisper-large-v3-turbo").strip()
    if model not in STT_MODELS:
        raise VoiceError("STT_MODEL має бути whisper-large-v3-turbo або whisper-large-v3.")
    language = config.get("STT_LANGUAGE", "").strip()
    if language and not re.fullmatch(r"[a-z]{2}", language):
        raise VoiceError("STT_LANGUAGE: залиш порожнім для автовизначення або вкажи код, наприклад uk.")
    if filename not in {"voice.ogg", "voice.mp3", "voice.m4a", "voice.wav", "voice.webm", "voice.mp4"}:
        raise VoiceError("Непідтримуваний формат голосового.")
    boundary = secrets.token_hex(24)
    fields = {"model": model, "response_format": "verbose_json", "temperature": "0"}
    if language:
        fields["language"] = language
    chunks = []
    for name, value in fields.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
    chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                   'Content-Type: application/octet-stream\r\n\r\n').encode())
    chunks.extend((audio, f'\r\n--{boundary}--\r\n'.encode()))
    request = Request("https://api.groq.com/openai/v1/audio/transcriptions", data=b"".join(chunks),
                      headers={"Authorization": "Bearer " + key,
                               "Content-Type": "multipart/form-data; boundary=" + boundary,
                               "User-Agent": "PersonalNewsBot/1.0"})
    # Never pass a Telegram download URL to Groq: that URL contains the bot token.
    try:
        with urlopen(request, timeout=120) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise VoiceError("Завелика відповідь сервісу розпізнавання.")
        result = json.loads(raw)
    except HTTPError as error:
        code = error.code
        error.close()
        if code == 429:
            raise VoiceError("Досягнуто аудіоліміту Groq. Спробуй пізніше.") from None
        if code in {401, 403}:
            raise VoiceError("Groq відхилив доступ до розпізнавання. Перевір ключ і дозвіл моделі Whisper.") from None
        if code in {400, 413, 422}:
            raise VoiceError("Сервіс не прийняв аудіо. Спробуй інше або коротше голосове.") from None
        raise VoiceError("Розпізнавання зараз недоступне. Спробуй пізніше.") from None
    except (OSError, ValueError):
        raise VoiceError("Не вдалося отримати розшифровку. Перевір зв’язок і спробуй ще раз.") from None
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise VoiceError("Не вдалося розібрати мовлення: текст порожній. Спробуй чіткіший запис.")
    return text.strip(), model


def voice_source(message):
    if message.get("voice"):
        return message
    quoted = message.get("reply_to_message", {})
    return quoted if quoted.get("voice") else None


def recognize(api, source, received_at=None):
    start = monotonic()
    received = received_at if received_at is not None else source.get("_bot_received_at")
    if isinstance(received, (int, float)) and math.isfinite(received) and received <= start:
        start = received
    voice = source["voice"]
    duration = float(voice.get("duration", 0))
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
        raise VoiceError("Надішли голосове тривалістю до 10 хвилин.")
    if voice.get("file_size", 0) > MAX_BYTES:
        raise VoiceError("Надішли голосове до 19 МБ і до 10 хвилин.")
    config = read_config()
    reading_wpm, playback_speed = preferences(config)
    if not config.get("GROQ_API_KEY", "").strip():
        raise VoiceError("Для розшифровки потрібен GROQ_API_KEY у .env.")
    audio, filename = api.download_voice(voice["file_id"], MAX_BYTES)
    text, model = transcribe(audio, filename, config)
    return {"text": text, "model": model, "duration": duration, "started": start,
            "reading_wpm": reading_wpm, "playback_speed": playback_speed}


def safe_error(error):
    print("Voice transcription failed (" + type(error).__name__ + ").", file=sys.stderr)
    return str(error) if isinstance(error, VoiceError) else "Не вдалося завантажити або розшифрувати голосове. Перешли його в особистий чат бота й спробуй ще раз."


def record_run(db, result, context="private"):
    if db is None:
        return
    processing = monotonic() - result["started"]
    words = len(WORDS.findall(result["text"]))
    duration, reading_wpm, playback_speed = (result[key] for key in ("duration", "reading_wpm", "playback_speed"))
    try:
        with db:
            db.execute("""INSERT INTO voice_runs (completed_at, audio_seconds, word_count, processing_seconds,
                reading_wpm, playback_speed, estimated_saved_seconds, stt_model, context) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(), duration, words, processing, reading_wpm, playback_speed,
                 estimate(duration, words, processing, reading_wpm, playback_speed)["saved_seconds"], result["model"], context))
    except Exception as error:
        print("Voice metrics not saved (" + type(error).__name__ + ").", file=sys.stderr)


def formatted_transcript(text):
    return "<b>Розшифровка</b>\n" + escape(text, quote=False)


def fits_message(text):
    return len(("Розшифровка\n" + text).encode("utf-16-le")) <= 8192


def handle_voice(api, chat_id, message, db, conversations):
    source = voice_source(message)
    try:
        result = recognize(api, source, received_at=message.get("_bot_received_at"))
    except Exception as error:
        api.send(chat_id, safe_error(error))
        return
    text = result["text"]
    if fits_message(text):
        api.send(chat_id, formatted_transcript(text), parse_mode="HTML", link_preview_options={"is_disabled": True})
    else:
        # One complete document instead of many chat bubbles or a truncated transcript.
        html = ('<!doctype html><html lang="uk"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Розшифровка</title><body><h1>Розшифровка</h1>'
                '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + escape(text) + '</pre></body></html>')
        api.send_document(chat_id, "transcript.html", html)
    record_run(db, result)
    context_label = ("Лише початок голосового, перші 3500 символів; решта тексту недоступна в контексті."
                     if len(text) > 3500 else "Текст голосового повідомлення.")
    conversations[chat_id] = [
        {"role": "user", "content": context_label + " Матеріал для наступного запитання:\n" + text[:3500]},
        {"role": "assistant", "content": "Розшифровку отримано. Можу допомогти з цим текстом."},
    ]
    conversations.move_to_end(chat_id)
    while len(conversations) > 32:
        conversations.popitem(last=False)


def handle_quoted_voice(api, message, db, reply):
    try:
        result = recognize(api, voice_source(message), received_at=message.get("_bot_received_at"))
    except Exception as error:
        reply(message, safe_error(error))
        return
    if not fits_message(result["text"]):
        # Guest Mode permits one result. Never silently truncate a transcript or
        # send a document to a guest chat's numeric ID using ordinary sendMessage.
        reply(message, "Розшифровка довша за ліміт одного гостьового повідомлення. Перешли голосове в особистий чат бота — там отримаєш повний текст файлом.")
        return
    reply(message, formatted_transcript(result["text"]), formatted=True)
    record_run(db, result, context="guest" if message.get("guest_query_id") else "group")
