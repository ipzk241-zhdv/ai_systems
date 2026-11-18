"""Shared private file storage. Telegram identities and passwords are verified in code."""
import hashlib
import hmac
import os
from io import BytesIO
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time

from settings import BASE_DIR, read_config


MAX_FILE_BYTES = 2048 * 1024 * 1024
MAX_STORAGE_BYTES = 5120 * 1024 * 1024
SESSION_SECONDS = 2 * 60 * 60


class FileError(ValueError):
    pass


class FileConflict(FileError):
    pass


def password_hash(password):
    if not 12 <= len(password) <= 256:
        raise FileError("Пароль має містити від 12 до 256 символів.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 600000).hex()
    return "pbkdf2_sha256$600000$" + salt + "$" + digest


def password_valid(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        rounds = int(rounds)
        if algorithm != "pbkdf2_sha256" or not 600000 <= rounds <= 2000000 or len(password) > 256:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def fingerprint(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class FileStore:
    def __init__(self, root=None, config=read_config):
        self.root = Path(root) if root else BASE_DIR / ".runtime" / "files"
        self.blobs = self.root / "blobs"
        self.config = config

    def enabled(self):
        return self.config().get("FILES_ENABLED", "").strip() == "1"

    def allowed(self, user_id):
        if type(user_id) is not int or user_id <= 0 or not self.enabled():
            return False
        values = re.split(r"[,\s]+", self.config().get("FILES_ALLOWED_USER_IDS", "").strip())
        return str(user_id) in {value for value in values if re.fullmatch(r"[1-9][0-9]*", value)}

    def require_user(self, user_id):
        if not self.allowed(user_id):
            raise FileError("Доступ до сховища не дозволений для твого Telegram user_id.")

    def limits(self):
        config = self.config()
        def value(key, default, maximum):
            raw = config.get(key, "").strip()
            if not raw:
                return default
            try:
                number = int(raw)
            except (ValueError, TypeError):
                raise FileError(key + " має бути цілим числом у МБ.") from None
            if not 1 <= number <= maximum:
                raise FileError(key + " має бути від 1 до " + str(maximum) + " МБ.")
            return number * 1024 * 1024
        try:
            max_items = int(config.get("FILES_MAX_UPLOAD_ITEMS", "").strip() or "10000")
        except (ValueError, TypeError):
            raise FileError("FILES_MAX_UPLOAD_ITEMS має бути цілим числом.") from None
        if not 1 <= max_items <= 100000:
            raise FileError("FILES_MAX_UPLOAD_ITEMS має бути від 1 до 100000.")
        return {"max_upload_items": max_items, "max_file_bytes": value("FILES_MAX_FILE_MB", MAX_FILE_BYTES, 16384),
                "limit_bytes": value("FILES_STORAGE_LIMIT_MB", MAX_STORAGE_BYTES, 1048576)}

    def connect(self):
        self.blobs.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.root / "files.sqlite3"), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY, blob TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT NOT NULL, uploaded_by INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_files (
                file_id INTEGER NOT NULL REFERENCES files(id), bot_key TEXT NOT NULL, blob TEXT NOT NULL,
                name TEXT NOT NULL, telegram_id TEXT NOT NULL, kind TEXT NOT NULL,
                PRIMARY KEY(file_id,bot_key)
            );
            CREATE TABLE IF NOT EXISTS telegram_saves (
                request_key TEXT PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id)
            );
            CREATE TABLE IF NOT EXISTS grants (
                hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL,
                password_tag TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_failures (at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES folders(id),
                name TEXT NOT NULL, name_key TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS folders_unique_name
                ON folders(COALESCE(parent_id, 0), name_key);
        """)
        if "folder_id" not in {row[1] for row in db.execute("PRAGMA table_info(files)")}:
            db.execute("ALTER TABLE files ADD COLUMN folder_id INTEGER REFERENCES folders(id)")
        db.execute("CREATE INDEX IF NOT EXISTS files_folder ON files(folder_id)")
        db.commit()
        return db

    def issue_grant(self, user_id):
        self.require_user(user_id)
        token = secrets.token_urlsafe(32)
        db = self.connect()
        try:
            with db:
                db.execute("DELETE FROM grants WHERE expires < ?", (time.time(),))
                db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
                db.execute("INSERT INTO grants VALUES (?, ?, ?)", (fingerprint(token), user_id, time.time() + 300))
        finally:
            db.close()
        return token

    def login(self, grant, password):
        if not isinstance(grant, str) or not isinstance(password, str) or len(grant) > 100:
            raise FileError("Перевір пароль і отримай нове посилання через /files у боті.")
        db = self.connect()
        try:
            recent = time.time() - 300
            db.execute("DELETE FROM login_failures WHERE at < ?", (recent,))
            db.commit()
            if db.execute("SELECT count(*) FROM login_failures").fetchone()[0] >= 20:
                raise FileError("Забагато невдалих спроб. Зачекай 5 хвилин.")
            row = db.execute("SELECT user_id, expires FROM grants WHERE hash=?", (fingerprint(grant),)).fetchone()
            valid = bool(row and row[1] > time.time() and self.allowed(row[0]))
            # The grant is random and short-lived; the common password is a second factor.
            valid = password_valid(password, self.config().get("FILES_PASSWORD_HASH", "")) and valid
            if not valid:
                with db:
                    db.execute("INSERT INTO login_failures VALUES (?)", (time.time(),))
                raise FileError("Перевір пароль і отримай нове посилання через /files у боті.")
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            with db:
                cursor = db.execute("DELETE FROM grants WHERE hash=? AND expires>?", (fingerprint(grant), time.time()))
                if cursor.rowcount != 1:
                    raise FileError("Посилання вже використано. Отримай нове через /files.")
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                           (fingerprint(session), row[0], csrf, time.time() + SESSION_SECONDS,
                            fingerprint(self.config().get("FILES_PASSWORD_HASH", ""))))
            return session, csrf
        finally:
            db.close()

    def session(self, token):
        if not isinstance(token, str) or not token or len(token) > 100:
            raise FileError("Увійди через нове посилання з команди /files у боті.")
        db = self.connect()
        try:
            row = db.execute("SELECT user_id, csrf, expires, password_tag FROM sessions WHERE hash=?", (fingerprint(token),)).fetchone()
        finally:
            db.close()
        if not row or row[2] <= time.time() or not hmac.compare_digest(row[3], fingerprint(self.config().get("FILES_PASSWORD_HASH", ""))):
            raise FileError("Сесія завершилась. Отримай нове посилання через /files.")
        self.require_user(row[0])
        return row[0], row[1]

    def logout(self, token):
        db = self.connect()
        try:
            with db:
                db.execute("DELETE FROM sessions WHERE hash=?", (fingerprint(token),))
        finally:
            db.close()

    def list_files(self, user_id):
        self.require_user(user_id)
        db = self.connect()
        try:
            rows = db.execute("SELECT id, name, size, created_at, folder_id FROM files ORDER BY id DESC").fetchall()
            return [dict(zip(("id", "name", "size", "created_at", "folder_id"), row)) for row in rows]
        finally:
            db.close()

    def storage_info(self, user_id):
        self.require_user(user_id)
        db = self.connect()
        try:
            used = db.execute("SELECT COALESCE(sum(size), 0) FROM files").fetchone()[0]
        finally:
            db.close()
        limits = self.limits()
        available = min(limits["limit_bytes"] - used, shutil.disk_usage(str(self.root)).free - 64 * 1024 * 1024)
        return {**limits, "used_bytes": used, "available_bytes": max(0, available)}

    @staticmethod
    def valid_name(name):
        name = name.strip() if isinstance(name, str) else name
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or len(name) > 180 or "/" in name or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise FileError("Некоректна назва; максимум 180 символів, без слешів.")
        return name.strip()

    @staticmethod
    def folder_path(db, folder_id):
        path, visited = [], set()
        while folder_id is not None:
            if type(folder_id) is not int or not 1 <= folder_id < 2 ** 63 or folder_id in visited or len(visited) >= 32:
                raise FileError("Некоректна папка або завелика глибина вкладення.")
            visited.add(folder_id)
            row = db.execute("SELECT id, name, parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if not row:
                raise FileError("Папку не знайдено.")
            path.append({"id": row[0], "name": row[1]})
            folder_id = row[2]
        return list(reversed(path))

    def list_folders(self, user_id):
        self.require_user(user_id)
        db = self.connect()
        try:
            rows = db.execute("SELECT id, parent_id, name, created_at FROM folders ORDER BY name_key, id").fetchall()
            return [dict(zip(("id", "parent_id", "name", "created_at"), row)) for row in rows]
        finally:
            db.close()

    def create_folder(self, user_id, name, parent_id=None):
        self.require_user(user_id)
        name = self.valid_name(name)
        db = self.connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if len(self.folder_path(db, parent_id)) >= 32:
                    raise FileError("Максимальна глибина папок — 32 рівні.")
                cursor = db.execute("INSERT INTO folders (parent_id,name,name_key,created_at) VALUES (?,?,?,?)",
                                    (parent_id, name, name.casefold(), time.time()))
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            raise FileError("У цій папці вже є папка з такою назвою.") from None
        finally:
            db.close()

    def import_folders(self, user_id, directories, files, parent_id=None):
        """Validate the complete manifest, then create its folders in one transaction."""
        self.require_user(user_id)
        limits = self.limits()
        max_items = limits["max_upload_items"]
        if (not isinstance(directories, list) or not isinstance(files, list)
                or not 1 <= len(directories) + len(files) <= max_items):
            raise FileError("Обери папку з не більш ніж " + str(max_items) + " файлів і підпапок.")
        paths, canonical, file_paths, total = {}, {}, set(), 0

        def parts(path, is_file=False):
            if (not isinstance(path, str) or not path or len(path) > 6000 or "\\" in path
                    or path.startswith("/") or re.match(r"^[A-Za-z]:", path)):
                raise FileError("Некоректний відносний шлях у папці.")
            result = path.split("/")
            if len(result) > (33 if is_file else 32):
                raise FileError("Максимальна глибина папок — 32 рівні.")
            for name in result:
                self.valid_name(name)
            return result

        def add_directory(names):
            for depth in range(1, len(names) + 1):
                path = "/".join(names[:depth])
                key = "/".join(self.valid_name(name).casefold() for name in names[:depth])
                if key in canonical and canonical[key] != path:
                    raise FileError("У вибраній папці є неоднозначні назви підпапок.")
                canonical[key], paths[path] = path, names[:depth]
                if len(paths) + len(files) > max_items:
                    raise FileError("Обери папку з не більш ніж " + str(max_items) + " файлів і підпапок.")

        for path in directories:
            add_directory(parts(path))
        for entry in files:
            if not isinstance(entry, dict):
                raise FileError("Некоректний список файлів папки.")
            names = parts(entry.get("path"), is_file=True)
            length = entry.get("size")
            if type(length) is not int or length < 0 or length > limits["max_file_bytes"]:
                raise FileError("Один із файлів перевищує налаштований максимальний розмір.")
            if entry["path"] in file_paths:
                raise FileError("У списку є повторний шлях до файла.")
            file_paths.add(entry["path"])
            total += length
            add_directory(names[:-1])
        if len(paths) + len(files) > max_items:
            raise FileError("Обери папку з не більш ніж " + str(max_items) + " файлів і підпапок.")
        if set(paths) & file_paths:
            raise FileError("Файл і папка мають однаковий шлях.")
        db = self.connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                base_depth = len(self.folder_path(db, parent_id))
                if any(base_depth + len(names) > 32 for names in paths.values()):
                    raise FileError("Максимальна глибина папок — 32 рівні.")
                used = db.execute("SELECT COALESCE(sum(size),0) FROM files").fetchone()[0]
                if used + total > limits["limit_bytes"]:
                    raise FileError("Для всієї папки не вистачає місця у сховищі.")
                largest = max((entry["size"] for entry in files), default=0)
                if shutil.disk_usage(str(self.root)).free < total + largest + 64 * 1024 * 1024:
                    raise FileError("На диску сервера недостатньо місця для цієї папки.")
                mapping, roots = {}, []
                for path, names in sorted(paths.items(), key=lambda item: (len(item[1]), item[0])):
                    parent = mapping["/".join(names[:-1])] if len(names) > 1 else parent_id
                    name = self.valid_name(names[-1])
                    if len(names) == 1:
                        existing = {row[0] for row in db.execute("SELECT name_key FROM folders WHERE parent_id IS ?", (parent,))}
                        base, number = name, 1
                        while name.casefold() in existing:
                            number += 1
                            suffix = " (" + str(number) + ")"
                            name = base[:180 - len(suffix)].rstrip() + suffix
                    cursor = db.execute("INSERT INTO folders (parent_id,name,name_key,created_at) VALUES (?,?,?,?)",
                                        (parent, name, name.casefold(), time.time()))
                    mapping[path] = cursor.lastrowid
                    if len(names) == 1:
                        roots.append({"id": cursor.lastrowid, "name": name})
                return {"folders": mapping, "roots": roots}
        finally:
            db.close()

    def update_item(self, user_id, kind, item_id, name, parent_id=None):
        self.require_user(user_id)
        name = self.valid_name(name)
        if kind not in {"file", "folder"} or type(item_id) is not int or not 1 <= item_id < 2 ** 63:
            raise FileError("Об’єкт не знайдено.")
        db = self.connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                parents = self.folder_path(db, parent_id)
                if kind == "folder":
                    if item_id in {folder["id"] for folder in parents}:
                        raise FileError("Не можна перемістити папку в неї саму або її підпапку.")
                    children = {}
                    for node, parent in db.execute("SELECT id,parent_id FROM folders"):
                        children.setdefault(parent, []).append(node)
                    pending = [(item_id, 1)]
                    while pending:
                        node, depth = pending.pop()
                        if len(parents) + depth > 32:
                            raise FileError("Максимальна глибина папок — 32 рівні.")
                        pending.extend((child, depth + 1) for child in children.get(node, []))
                    cursor = db.execute("UPDATE folders SET name=?,name_key=?,parent_id=? WHERE id=?",
                                        (name, name.casefold(), parent_id, item_id))
                else:
                    cursor = db.execute("UPDATE files SET name=?,folder_id=? WHERE id=?", (name, parent_id, item_id))
                if cursor.rowcount != 1:
                    raise FileError("Об’єкт не знайдено.")
        except sqlite3.IntegrityError:
            raise FileError("У цій папці вже є папка з такою назвою.") from None
        finally:
            db.close()

    def upload(self, user_id, name, data, folder_id=None):
        return self.upload_stream(user_id, name, BytesIO(data), len(data), folder_id)

    def upload_stream(self, user_id, name, stream, length, folder_id=None, unique=False, request_key=None):
        self.require_user(user_id)
        name = str(name).replace("\\", "/").rsplit("/", 1)[-1].strip()
        name = self.valid_name(name)
        limits = self.limits()
        if type(length) is not int or length < 0 or length > limits["max_file_bytes"]:
            raise FileError("Файл перевищує налаштований максимальний розмір.")
        db = self.connect()
        blob = secrets.token_hex(24)
        path = self.blobs / blob
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if request_key:
                    previous = db.execute("SELECT file_id FROM telegram_saves WHERE request_key=?", (request_key,)).fetchone()
                    if previous:
                        return previous[0]
                self.folder_path(db, folder_id)
                if unique:
                    existing = {row[0].casefold() for row in db.execute("SELECT name FROM files WHERE folder_id IS ?", (folder_id,))}
                    base, suffix, number = Path(name).stem, Path(name).suffix, 1
                    if len(suffix) > 40:
                        base, suffix = name, ""
                    while name.casefold() in existing:
                        number += 1
                        tail = " (" + str(number) + ")" + suffix
                        name = base[:180-len(tail)] + tail
                total = db.execute("SELECT COALESCE(sum(size), 0) FROM files").fetchone()[0]
                if total + length > limits["limit_bytes"]:
                    raise FileError("Недостатньо місця в межах налаштованого обсягу сховища.")
                if shutil.disk_usage(str(self.root)).free < length + 64 * 1024 * 1024:
                    raise FileError("На диску сервера недостатньо вільного місця.")
                digest, remaining = hashlib.sha256(), length
                with path.open("xb") as output:
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise FileError("Файл завантажився не повністю. Спробуй ще раз.")
                        output.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                cursor = db.execute("INSERT INTO files (blob, name, size, sha256, uploaded_by, created_at,folder_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (blob, name, length, digest.hexdigest(), user_id, time.time(), folder_id))
                if request_key:
                    db.execute("INSERT INTO telegram_saves(request_key,file_id) VALUES (?,?)", (request_key, cursor.lastrowid))
                return cursor.lastrowid
        except Exception:
            if path.exists():
                path.unlink()
            raise
        finally:
            db.close()

    def telegram_remember(self, user_id, file_id, bot_key, telegram_id, kind, expected_blob=None, expected_name=None, expected_digest=None):
        path, name, _ = self.get_file(user_id, file_id)
        if kind not in {"document", "photo", "video", "audio", "voice"}:
            raise FileError("Непідтримуваний Telegram-файл.")
        db = self.connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT blob,name,sha256 FROM files WHERE id=?", (file_id,)).fetchone()
                if (not row or row[0] != (expected_blob or path.name) or row[1] != (expected_name or name)
                        or (expected_digest is not None and row[2] != expected_digest)):
                    raise FileConflict("Файл змінився під час завантаження в Telegram. Повтори команду для актуальної версії.")
                db.execute("INSERT OR REPLACE INTO telegram_files(file_id,bot_key,blob,name,telegram_id,kind) VALUES (?,?,?,?,?,?)",
                           (file_id, bot_key, row[0], row[1], telegram_id, kind))
        finally:
            db.close()

    def telegram_cached(self, user_id, file_id, bot_key, expected_blob=None):
        path, name, _ = self.get_file(user_id, file_id)
        if expected_blob is not None and path.name != expected_blob:
            raise FileConflict("Файл змінився; повтори команду для актуальної версії.")
        db = self.connect()
        try:
            row = db.execute("SELECT telegram_id,kind FROM telegram_files WHERE file_id=? AND bot_key=? AND blob=? AND name=?",
                             (file_id, bot_key, path.name, name)).fetchone()
            return {"telegram_id":row[0], "kind":row[1]} if row else None
        finally:
            db.close()

    def text_preview(self, user_id, file_id):
        path, name, length = self.get_file(user_id, file_id)
        if length > 1024 * 1024:
            raise FileError("Текстовий перегляд і редагування — для файлів до 1 МіБ. Оригінал можна скачати.")
        try:
            data = path.read_bytes()
            text = data.decode("utf-8-sig")
        except UnicodeError:
            raise FileError("Для текстового перегляду збережи файл у кодуванні UTF-8.") from None
        if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
            raise FileError("Файл містить двійкові дані; текстовий перегляд недоступний.")
        return {"kind": "text", "name": name, "text": text, "revision": fingerprint(path.name + ":" + name),
                "editable": Path(name).suffix.casefold() in {".txt", ".md"}}

    def replace_text(self, user_id, file_id, text, revision):
        self.require_user(user_id)
        if not isinstance(text, str) or not isinstance(revision, str) or len(text) > 1024 * 1024:
            raise FileError("Некоректний або завеликий текст.")
        db = self.connect()
        new_path = self.blobs / secrets.token_hex(24)
        old_path = None
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                old_path, name, old_size = self.get_file(user_id, file_id)
                if Path(name).suffix.casefold() not in {".txt", ".md"}:
                    raise FileError("Редагувати можна лише TXT і MD.")
                if not hmac.compare_digest(fingerprint(old_path.name + ":" + name), revision):
                    raise FileConflict("Файл уже змінився в іншій вкладці або іншим користувачем. Скопіюй свої правки й відкрий актуальну версію.")
                if old_size > 1024 * 1024:
                    raise FileError("Редагування доступне для файлів до 1 МіБ.")
                old_data = old_path.read_bytes()
                try:
                    old_data.decode("utf-8-sig")
                    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
                    if b"\r\n" in old_data:
                        normalized = normalized.replace("\n", "\r\n")
                    data = (b"\xef\xbb\xbf" if old_data.startswith(b"\xef\xbb\xbf") else b"") + normalized.encode("utf-8")
                except UnicodeError:
                    raise FileError("Для редагування потрібен текст UTF-8.") from None
                limits = self.limits()
                if len(data) > min(1024 * 1024, limits["max_file_bytes"]):
                    raise FileError("Збережений текст має бути до 1 МіБ і в межах ліміту на файл.")
                total = db.execute("SELECT COALESCE(sum(size),0) FROM files").fetchone()[0]
                if total - old_size + len(data) > limits["limit_bytes"]:
                    raise FileError("Для зміненого файла не вистачає місця у сховищі.")
                if shutil.disk_usage(str(self.root)).free < len(data) + 64 * 1024 * 1024:
                    raise FileError("На диску сервера недостатньо місця.")
                with new_path.open("xb") as output:
                    output.write(data); output.flush(); os.fsync(output.fileno())
                self.require_user(user_id)
                db.execute("UPDATE files SET blob=?,size=?,sha256=? WHERE id=?",
                           (new_path.name, len(data), hashlib.sha256(data).hexdigest(), file_id))
            result = {"revision": fingerprint(new_path.name + ":" + name), "size": len(data)}
        except Exception:
            if new_path.exists():
                new_path.unlink()
            raise
        finally:
            db.close()
        # The committed new blob is complete; existing open downloads can finish on Linux.
        try:
            old_path.unlink()
        except OSError:
            pass
        return result

    def get_file(self, user_id, file_id):
        self.require_user(user_id)
        if type(file_id) is not int or not 1 <= file_id < 2 ** 63:
            raise FileError("Файл не знайдено.")
        db = self.connect()
        try:
            row = db.execute("SELECT blob, name, size FROM files WHERE id=?", (int(file_id),)).fetchone()
        finally:
            db.close()
        if not row or not re.fullmatch(r"[0-9a-f]{48}", row[0]):
            raise FileError("Файл не знайдено.")
        path = self.blobs / row[0]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row[2]:
            raise FileError("Файл недоступний.")
        return path, row[1], row[2]


if __name__ == "__main__":
    import getpass
    password = getpass.getpass("Пароль сховища (від 12 символів): ")
    if password != getpass.getpass("Повтори пароль: "):
        raise SystemExit("Паролі не збігаються.")
    print("FILES_PASSWORD_HASH=" + password_hash(password))
