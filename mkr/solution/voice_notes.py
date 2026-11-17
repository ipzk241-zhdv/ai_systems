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


def text_chunks(text, size=1500):
    while text:
        cut = min(size, len(text))
        if cut < len(text):
            boundary = max(text.rfind(" ", 0, cut), text.rfind("\n", 0, cut))
            if boundary > cut // 2:
                cut = boundary + 1
        yield text[:cut]
        text = text[cut:]


def statistics_text(duration, words, processing, reading_wpm, playback_speed):
    values = estimate(duration, words, processing, reading_wpm, playback_speed)
    saved = values["saved_seconds"]
    label = (f"Оціночна економія: <b>{saved:.0f} с</b>." if saved >= 0 else
             f"За цих припущень прослухати швидше приблизно на <b>{-saved:.0f} с</b>.")
    return (f"Аудіо: {duration:g} с · слів у розшифровці: {words}.\n"
            f"Від отримання ботом до надсилання тексту: {processing:.1f} с.\n"
            f"Припущення: читання {reading_wpm:g} слів/хв, прослуховування {playback_speed:g}×.\n"
            f"Читання ≈ {values['reading_seconds']:.0f} с; обробка + читання ≈ {values['total_seconds']:.0f} с.\n"
            f"{label}\nЦе розрахункова оцінка, не замір твоєї швидкості. Вона не враховує завантаження "
            "голосового з телефона в Telegram, доставку цієї статистики та виправлення помилок. "
            "Імена, числа й важливі деталі перевір за аудіо.")


def handle_voice(api, chat_id, message, db, conversations):
    start = monotonic()
    received = message.get("_bot_received_at")
    if isinstance(received, (int, float)) and math.isfinite(received) and received <= start:
        start = received
    voice = message["voice"]
    try:
        duration = float(voice.get("duration", 0))
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
            raise VoiceError("Надішли голосове тривалістю до 10 хвилин.")
        if voice.get("file_size", 0) > MAX_BYTES:
            raise VoiceError("Надішли голосове до 19 МБ і до 10 хвилин.")
        config = read_config()
        reading_wpm, playback_speed = preferences(config)
        if not config.get("GROQ_API_KEY", "").strip():
            raise VoiceError("Для розшифровки потрібен GROQ_API_KEY у .env.")
        api.send(chat_id, "Розшифровую голосове… Аудіо буде передано Groq для розпізнавання.")
        audio, filename = api.download_voice(voice["file_id"], MAX_BYTES)
        text, model = transcribe(audio, filename, config)
    except Exception as error:
        print("Voice transcription failed (" + type(error).__name__ + ").", file=sys.stderr)
        safe = str(error) if isinstance(error, VoiceError) else "Не вдалося завантажити або розшифрувати голосове. Спробуй ще раз."
        api.send(chat_id, safe)
        return
    for index, part in enumerate(text_chunks(text), 1):
        prefix = "<b>Розшифровка</b>\n" if index == 1 else ""
        api.send(chat_id, prefix + escape(part, quote=False), parse_mode="HTML",
                 link_preview_options={"is_disabled": True})
    processing = monotonic() - start
    words = len(WORDS.findall(text))
    stats = statistics_text(duration, words, processing, reading_wpm, playback_speed)
    if len(text) > 3500:
        stats += "\nДля наступних запитань у пам’яті чату лише перші 3500 символів. Повна розшифровка — вище."
    api.send(chat_id, stats, parse_mode="HTML")
    # Persist numeric measurements only. Audio and transcript are not written to disk.
    try:
        with db:
            db.execute("""INSERT INTO voice_runs (completed_at, audio_seconds, word_count, processing_seconds,
                reading_wpm, playback_speed, estimated_saved_seconds, stt_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(), duration, words, processing, reading_wpm, playback_speed,
                 estimate(duration, words, processing, reading_wpm, playback_speed)["saved_seconds"], model))
    except Exception as error:
        print("Voice metrics not saved (" + type(error).__name__ + ").", file=sys.stderr)
    context_label = ("Лише початок голосового, перші 3500 символів; решта тексту недоступна в контексті."
                     if len(text) > 3500 else "Текст голосового повідомлення.")
    conversations[chat_id] = [
        {"role": "user", "content": context_label + " Матеріал для наступного запитання:\n" + text[:3500]},
        {"role": "assistant", "content": "Розшифровку отримано. Можу допомогти з цим текстом."},
    ]
    conversations.move_to_end(chat_id)
    while len(conversations) > 32:
        conversations.popitem(last=False)
