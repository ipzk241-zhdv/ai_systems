"""Verify AI configuration with one tiny request, without starting Telegram polling."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "solution"))

from llm import chat, error_message
from settings import default_model, provider, read_config


def main():
    try:
        config = read_config()
        model = default_model(config)
        print("AI: " + provider(config) + " / " + model, flush=True)
        result = chat([
            {"role": "system", "content": "Поверни JSON: status має бути ok."},
            {"role": "user", "content": "Перевірка з'єднання."},
        ], model, options={"temperature": 0, "num_predict": 150}, think=False,
            schema={"type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
                    "required": ["status"], "additionalProperties": False})
        if result.get("done_reason") == "length" or json.loads(result["message"]["content"]) != {"status": "ok"}:
            raise ValueError("Invalid response")
        print("OK: connection and structured JSON response.")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(error_message(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
