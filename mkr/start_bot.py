import sys
from pathlib import Path


if __name__ == "__main__":
    solution = Path(__file__).resolve().parent / "solution"
    sys.path.insert(0, str(solution))

    from telegram_bot import main

    raise SystemExit(main())
