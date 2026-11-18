# Прямий HTTPS для файлового сховища

Поточна адреса: **https://141.147.21.157**. Вхід — через нове посилання `/files`
у боті та пароль. Без одноразового посилання сторінка лише підказує отримати його.
SSH-тунель і Cloudflare для цього доступу не потрібні.

## Як працює

Nginx приймає HTTPS на порту 443 і передає запити Python-сховищу на `127.0.0.1:8080`.
Порт 80 віддає тільки перевірки ACME та перенаправлення на HTTPS. На Oracle у Security
List дозволені вхідні TCP 80 і 443 із `0.0.0.0/0`; правила ОС відкривають ті самі
порти. Доступ до файлів, whitelist, пароль, одноразові посилання та CSRF залишаються
в Python-застосунку. Порт 8080 назовні не відкривається.

Використовується довірений IP-сертифікат Let's Encrypt із профілем `shortlived`.
Таймер `news-files-cert-renew.timer` перевіряє поновлення кожні 6 годин і після
успішного поновлення перезавантажує конфігурацію Nginx. Перевірку `renew --dry-run`
успішно виконано під час налаштування. Сертифікат не потребує ручного підтвердження
попереджень браузера. Якщо публічна IP-адреса VM зміниться, потрібен новий сертифікат
і відповідне оновлення адреси.

## Налаштування й команди

- Конфігурація Nginx: `/etc/nginx/sites-available/news-files`.
- Сертифікат: `/etc/letsencrypt/live/news-files-ip/` (ключ не копіювати в Git).
- Certbot із власними залежностями: `/opt/news-files-certbot`; запуск через
  `/usr/local/bin/news-files-certbot`. Це не змінює залежності самого бота.
- Допоміжний скрипт: `/usr/local/lib/news-bot/direct_files_https.py`.
- Адреса для бота: `FILES_PUBLIC_URL=https://141.147.21.157` у `solution/.env`.

```sh
sudo systemctl status nginx news-bot --no-pager
sudo systemctl list-timers news-files-cert-renew.timer
sudo journalctl -u news-files-cert-renew -n 30 --no-pager
```

Перевірка поновлення сертифіката без заміни чинного:

```sh
sudo /usr/local/bin/news-files-certbot renew --cert-name news-files-ip --dry-run
```

Після зміни `FILES_PORT` або `FILES_MAX_FILE_MB` у `.env`:

```sh
sudo systemctl restart news-bot
sudo python3 /home/ubuntu/ai_systems/mkr/deploy/direct_files_https.py --refresh-proxy
```

Так оновлюється також ліміт Nginx; сам перезапуск бота його не змінює.
Команда використовує актуальний скрипт із репозиторію, щоб збільшена межа файла
не впиралась у стару встановлену версію інфраструктурного скрипта.
Загальна квота `FILES_STORAGE_LIMIT_MB` не потребує змін Nginx.

## Повторне встановлення на цій VM

Скрипт `direct_files_https.py` призначений для поточного шляху репозиторію й IP.
Підготовка встановлює Nginx із Ubuntu та Certbot 5.4+ у власний каталог, налаштовує
перевірку ACME, правила ОС та служби. Активація перевіряє сертифікат у staging,
отримує довірений сертифікат, вмикає поновлення і змінює тільки `FILES_PUBLIC_URL`
в `.env`, зберігаючи власника й решту налаштувань. Видача сертифіката передбачає
прийняття умов Let's Encrypt від імені власника сервера.

```sh
sudo python3 deploy/direct_files_https.py --prepare
# Дозволити TCP 80/443 у Security List Oracle, якщо це ще не зроблено.
sudo python3 deploy/direct_files_https.py --activate
```

Підготовка/активація — інфраструктурні дії, не частина автоматичного CI-деплою.
Звичайний `git push` не перевстановлює вебсервер і не запитує новий сертифікат.
Тестовий Cloudflare-тунель `news-files-tunnel.service` вимкнений: із мереж користувача
його адреса була недоступна, тому він більше не оновлює `FILES_PUBLIC_URL`.

Офіційна документація: [IP-сертифікати Let's Encrypt і Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot).
