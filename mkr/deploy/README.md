# Oracle Cloud + Groq

For the running Oracle VM and automatic GitHub deployments, see [CI/CD setup](CI-CD.md).

Oracle запускає Python-бота, Groq виконує AI-запити. Ollama та GPU на сервері не потрібні.
Поточний локальний запуск `python start_bot.py` залишається доступним.

## 1. Акаунти

- Oracle: https://signup.cloud.oracle.com/
- Groq: https://console.groq.com/ — після реєстрації https://console.groq.com/keys
- Ліміти свого Groq-акаунта: https://console.groq.com/settings/limits

Обирай Oracle **Always Free eligible**, а не просто ресурси, покриті пробними кредитами.
Home region обирається при реєстрації; Always Free VM створюється в ньому.
Доступність безкоштовних VM залежить від вільних ресурсів регіону. Oracle може
звільняти неактивні Always Free VM; це не гарантія безперервного безкоштовного хостингу.

## 2. Конфігурація

У `solution/.env` залиш свій TELEGRAM_BOT_TOKEN і додай:

```dotenv
LLM_PROVIDER=groq
GROQ_API_KEY=твій_ключ
LLM_MODEL=openai/gpt-oss-20b
```

Порожній шаблон: `solution/.env.example`. Значення середовища мають пріоритет над файлом.
Усі функції — відбір, повтори, пояснення, порівняння та звичайний чат — використовують
одного провайдера й модель. Назва моделі — ідентифікатор Groq, а не назва Ollama.
Для повернення до локального режиму: `LLM_PROVIDER=ollama`, `LLM_MODEL=qwen3.5:9b`.

Типово обрано GPT-OSS 20B: він підтримує strict JSON Schema для класифікації.
Іншу модель зі strict JSON Schema можна вказати в LLM_MODEL. Доступність і ліміти
перевір у Groq Console. Не кожна модель підтримує цей формат.

## 3. Перевірка без запуску Telegram

З папки `mkr`:

```console
python check_ai.py
```

Виконує один малий структурований AI-запит. Не запускає polling і не надсилає
повідомлень користувачам. Потребує дійсного Groq-ключа або запущеної Ollama.

## 4. VM в Oracle

Створи Compute Instance:

- Ubuntu 24.04 LTS, стандартний користувач `ubuntu`;
- shape з позначкою Always Free eligible: E2.1.Micro або A1.Flex у межах квоти акаунта;
- мінімальної пам'яті 1 GB достатньо для коду без локальної LLM;
- public subnet, public IPv4 та вихідний доступ до інтернету;
- SSH-порт 22 дозволений для твоєї IP-адреси. Збережи приватний SSH-ключ.

Порти 80/443 на вхід відкривати не треба: Telegram getUpdates працює через вихідні HTTPS-запити.

## 5. Передавання файлів

На своєму ПК, з папки `mkr`:

```console
python deploy/package.py
scp -i PATH_TO_PRIVATE_KEY news-bot.zip ubuntu@SERVER_IP:~/
```

Архів містить код і документацію; реальні ключі, .env та історія чатів у нього не потрапляють.
Замінюй PATH_TO_PRIVATE_KEY і SERVER_IP власними значеннями, шляхи з пробілами бери в лапки.

Підключись:

```console
ssh -i PATH_TO_PRIVATE_KEY ubuntu@SERVER_IP
```

На сервері встанови залежності з пакетів Ubuntu, без .venv:

```sh
sudo apt update
sudo apt install -y python3 python3-bs4 curl tzdata unzip
mkdir -p ~/news-bot
unzip -o ~/news-bot.zip -d ~/news-bot
mkdir -p ~/news-bot/solution/.runtime
cp ~/news-bot/solution/.env.example ~/news-bot/solution/.env
chmod 600 ~/news-bot/solution/.env
nano ~/news-bot/solution/.env
```

У nano внеси обидва ключі. Збереження Ctrl+O, Enter; вихід Ctrl+X.
Не повторюй `cp .../.env.example .../.env` під час оновлення — це перезапише ключі.

Для перенесення історії спершу зупини локального бота через Ctrl+C. Тоді з ПК:

```console
scp -i PATH_TO_PRIVATE_KEY solution/.runtime/bot-BOT_ID.sqlite3 ubuntu@SERVER_IP:~/news-bot/solution/.runtime/
```

BOT_ID — числова частина назви наявного файла. Це необов'язковий крок; без нього
на сервері почнеться нова історія. Базу копіюй лише коли обидва екземпляри бота зупинені.

## 6. Перевірка й автозапуск

На сервері:

```sh
cd ~/news-bot
python3 check_ai.py
```

Після успіху зупини бота на своєму ПК: одночасний polling одного токена спричиняє Telegram 409.
Запусти службу:

```sh
sudo cp deploy/news-bot.service /etc/systemd/system/news-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now news-bot
sudo systemctl status news-bot --no-pager
```

Служба розрахована на `/home/ubuntu/news-bot` і користувача `ubuntu`.
Для іншого користувача/шляху зміни User, Group, WorkingDirectory, ExecStart і ReadWritePaths у unit-файлі.

Логи / перезапуск / зупинка:

```sh
sudo journalctl -u news-bot -n 80 --no-pager
sudo systemctl restart news-bot
sudo systemctl stop news-bot
```

Після автозапуску SSH-консоль можна закрити. Служба запускається після перезавантаження VM.
Зміни конфігурації застосовуй через restart. Після кількох аварійних запусків:
`sudo systemctl reset-failed news-bot`, виправ конфігурацію та запусти службу знову.

## 7. Ліміти й дані

Один дайджест — кілька AI-запитів, не один. Groq обмежує запити й токени за хвилину/день.
Пакети для Groq зменшені; історія перевіряється частинами без відкидання старих кандидатів.
Між запитами код враховує заголовки залишку токенів; короткі 429/5xx повторює до двох разів.
При довгому ліміті бот повідомляє про нього, не перемикається на платний сервіс.
Бот відкритий для всіх приватних чатів: квота Groq спільна для всіх користувачів.

Для довгих статей Groq отримує перші 6000 символів, про що зазначається у відповіді.
Groq отримує тексти AI-запитів: заголовки, уривки статей і звичайні повідомлення користувача.
Telegram-токен та SQLite-файл не передаються в AI-запитах.
Ключі не потрапляють у deployment-архів або повідомлення про помилки.

Перевірка коду без ключів: з `solution` виконай `python -m unittest discover -p "test_*.py"`.

Офіційні джерела:
- https://www.oracle.com/cloud/free/
- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- https://console.groq.com/docs/models
- https://console.groq.com/docs/structured-outputs
- https://console.groq.com/docs/rate-limits
