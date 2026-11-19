"""Shared configuration for console tools and the Telegram bot."""
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
KEYS = {"TELEGRAM_BOT_TOKEN", "LLM_PROVIDER", "LLM_MODEL", "GROQ_API_KEY", "OLLAMA_MODEL",
        "STT_MODEL", "STT_LANGUAGE", "READING_WPM", "PLAYBACK_SPEED", "VISION_MODEL", "NEWS_POLL_MINUTES", "NEWS_MODEL",
        "FILES_ENABLED", "FILES_ALLOWED_USER_IDS", "FILES_PASSWORD_HASH", "FILES_PUBLIC_URL", "FILES_PORT",
        "FILES_STORAGE_LIMIT_MB", "FILES_MAX_FILE_MB", "FILES_MAX_UPLOAD_ITEMS",
        "FILES_TELEGRAM_CACHE_CHAT_ID"}


def read_config(path=None):
    values = {}
    try:
        for line in (path or BASE_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in KEYS:
                values[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return {**values, **{key: value for key, value in os.environ.items() if key in KEYS}}


def provider(config):
    name = config.get("LLM_PROVIDER", "ollama").strip().lower()
    if name not in {"ollama", "groq"}:
        raise ValueError("LLM_PROVIDER має бути ollama або groq.")
    return name


def default_model(config=None):
    config = read_config() if config is None else config
    if config.get("LLM_MODEL", "").strip():
        return config["LLM_MODEL"].strip()
    if provider(config) == "groq":
        return "openai/gpt-oss-20b"
    return config.get("OLLAMA_MODEL") or "qwen3.5:9b"


def news_model(fallback,config=None):
    config=read_config() if config is None else config
    return config.get("NEWS_MODEL", "").strip() or fallback
