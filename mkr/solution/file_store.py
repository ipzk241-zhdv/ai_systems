"""Shared private file storage. Telegram identities and passwords are verified in code."""
import hashlib
import hmac
from io import BytesIO
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time

from settings import BASE_DIR, read_config


MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_STORAGE_BYTES = 5120 * 1024 * 1024
SESSION_SECONDS = 2 * 60 * 60


class FileError(ValueError):
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
        return {"max_file_bytes": value("FILES_MAX_FILE_MB", MAX_FILE_BYTES, 1024),
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

    def upload_stream(self, user_id, name, stream, length, folder_id=None):
        self.require_user(user_id)
        name = str(name).replace("\\", "/").rsplit("/", 1)[-1].strip()
        name = self.valid_name(name)
        limits = self.limits()
        if type(length) is not int or length <= 0 or length > limits["max_file_bytes"]:
            raise FileError("Файл порожній або перевищує налаштований максимальний розмір.")
        db = self.connect()
        blob = secrets.token_hex(24)
        path = self.blobs / blob
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self.folder_path(db, folder_id)
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
                return cursor.lastrowid
        except Exception:
            if path.exists():
                path.unlink()
            raise
        finally:
            db.close()

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
