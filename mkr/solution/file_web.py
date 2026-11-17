"""Small WSGI file manager, served on loopback behind HTTPS by Waitress."""
from http.cookies import SimpleCookie
import hmac
import json
from pathlib import Path
import re
import sys
from threading import Thread
from urllib.parse import parse_qs, quote, urlsplit

from file_store import FileError, FileStore, SESSION_SECONDS


ASSETS = Path(__file__).resolve().parent / "file_web_assets"


class FileBody:
    def __init__(self, path, length):
        self.path, self.length = path, length

    def __iter__(self):
        with self.path.open("rb") as source:
            while True:
                chunk = source.read(256 * 1024)
                if not chunk:
                    break
                yield chunk


def public_origin(config):
    value = config.get("FILES_PUBLIC_URL", "").strip().rstrip("/")
    parts = urlsplit(value)
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parts.username or parts.password or parts.query or parts.fragment or parts.path
            or not parts.netloc or (parts.scheme != "https" and not (parts.scheme == "http" and local))):
        raise FileError("Вкажи FILES_PUBLIC_URL: HTTPS-адресу сайта або http://127.0.0.1:8080 для локального доступу.")
    return value


class FileApp:
    def __init__(self, store):
        self.store = store

    def __call__(self, env, start_response):
        headers = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                   ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
                   ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
        try:
            status, content_type, body, extra = self.dispatch(env)
            headers += extra
        except FileError as error:
            status, content_type = "403 Forbidden", "application/json; charset=utf-8"
            body = json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8")
        except (ValueError, KeyError, TypeError, UnicodeError):
            status, content_type, body = "400 Bad Request", "application/json", b'{"error":"Invalid request"}'
        except Exception as error:
            print("File web request failed (" + type(error).__name__ + ").", file=sys.stderr)
            status, content_type, body = "500 Internal Server Error", "application/json", b'{"error":"File service unavailable"}'
        headers += [("Content-Type", content_type), ("Content-Length", str(body.length if isinstance(body, FileBody) else len(body)))]
        start_response(status, headers)
        if env.get("REQUEST_METHOD") == "HEAD":
            return [b""]
        return body if isinstance(body, FileBody) else [body]

    def dispatch(self, env):
        if not self.store.enabled():
            raise FileError("Сховище вимкнене.")
        origin = public_origin(self.store.config())
        method, path = env.get("REQUEST_METHOD", "GET"), env.get("PATH_INFO", "/")
        if env.get("HTTP_HOST", "") != urlsplit(origin).netloc:
            raise FileError("Невідома адреса сайта.")
        if method in {"GET", "HEAD"} and path in {"/", "/app.css", "/app.js"}:
            filename, mime = {"/": ("index.html", "text/html; charset=utf-8"),
                              "/app.css": ("app.css", "text/css; charset=utf-8"),
                              "/app.js": ("app.js", "text/javascript; charset=utf-8")}[path]
            return "200 OK", mime, (ASSETS / filename).read_bytes(), []
        if method == "POST" and env.get("HTTP_ORIGIN") != origin:
            raise FileError("Запит має надходити з відкритого сайта сховища.")
        if method == "POST" and path == "/api/login":
            body = self.read_json(env, 4096)
            token, csrf = self.store.login(body.get("code"), body.get("password"))
            cookie = "files_session=" + token + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=" + str(SESSION_SECONDS)
            if origin.startswith("https:"):
                cookie += "; Secure"
            return self.json_response({"csrf": csrf}, [("Set-Cookie", cookie)])
        cookies = SimpleCookie()
        cookies.load(env.get("HTTP_COOKIE", ""))
        token = cookies["files_session"].value if "files_session" in cookies else ""
        user_id, csrf = self.store.session(token)
        if method == "POST" and not hmac.compare_digest(env.get("HTTP_X_CSRF_TOKEN", ""), csrf):
            raise FileError("Онови сторінку та повтори дію.")
        if method in {"GET", "HEAD"} and path == "/api/session":
            return self.json_response({"csrf": csrf, "user_id": user_id})
        if method in {"GET", "HEAD"} and path == "/api/files":
            return self.json_response({"files": self.store.list_files(user_id), "folders": self.store.list_folders(user_id),
                                       "storage": self.store.storage_info(user_id)})
        if method == "POST" and path == "/api/folders":
            body = self.read_json(env, 4096)
            folder_id = self.store.create_folder(user_id, body.get("name"), body.get("parent_id"))
            return self.json_response({"id": folder_id})
        match = re.fullmatch(r"/api/(files|folders)/([1-9][0-9]*)", path)
        if method == "POST" and match:
            body = self.read_json(env, 4096)
            self.store.update_item(user_id, "file" if match[1] == "files" else "folder", int(match[2]),
                                   body.get("name"), body.get("parent_id"))
            return self.json_response({"ok": True})
        if method == "POST" and path == "/api/logout":
            self.store.logout(token)
            return self.json_response({"ok": True}, [("Set-Cookie", "files_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
        if method == "POST" and path == "/api/files":
            query = parse_qs(env.get("QUERY_STRING", ""))
            name = query.get("name", [""])[0]
            folder_id = int(query["folder"][0]) if "folder" in query else None
            length = int(env.get("CONTENT_LENGTH") or "0")
            file_id = self.store.upload_stream(user_id, name, env["wsgi.input"], length, folder_id)
            return self.json_response({"id": file_id})
        match = re.fullmatch(r"/api/files/([1-9][0-9]*)/download", path)
        if method in {"GET", "HEAD"} and match:
            file_path, name, size = self.store.get_file(user_id, int(match[1]))
            disposition = "attachment; filename=download; filename*=UTF-8''" + quote(name, safe="")
            return "200 OK", "application/octet-stream", FileBody(file_path, size), [("Content-Disposition", disposition)]
        return "404 Not Found", "application/json", b'{"error":"Not found"}', []

    @staticmethod
    def read_body(env, limit):
        length = int(env.get("CONTENT_LENGTH") or "0")
        if length <= 0 or length > limit:
            raise FileError("Некоректний або завеликий розмір запиту.")
        data = env["wsgi.input"].read(length)
        if len(data) != length:
            raise FileError("Файл завантажився не повністю. Спробуй ще раз.")
        return data

    def read_json(self, env, limit):
        if not env.get("CONTENT_TYPE", "").startswith("application/json"):
            raise FileError("Очікується JSON-запит.")
        result = json.loads(self.read_body(env, limit).decode("utf-8"))
        if not isinstance(result, dict):
            raise FileError("Некоректний запит.")
        return result

    @staticmethod
    def json_response(value, headers=None):
        return "200 OK", "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode("utf-8"), headers or []


class FileWeb:
    def __init__(self, store=None):
        self.store = store or FileStore()
        self.server = None
        self.thread = None

    def start(self):
        if not self.store.enabled():
            return
        public_origin(self.store.config())
        if not self.store.config().get("FILES_PASSWORD_HASH", ""):
            raise FileError("Для сховища потрібен FILES_PASSWORD_HASH.")
        try:
            from waitress import create_server
        except ImportError:
            raise FileError("Для вебсховища потрібен Python 3.10+ та залежності з requirements-files.txt.") from None
        try:
            port = int(self.store.config().get("FILES_PORT", "8080"))
        except (ValueError, TypeError):
            raise FileError("FILES_PORT має бути від 1024 до 65535.") from None
        if not 1024 <= port <= 65535:
            raise FileError("FILES_PORT має бути від 1024 до 65535.")
        self.server = create_server(FileApp(self.store), host="127.0.0.1", port=port, threads=2,
                                    max_request_body_size=self.store.limits()["max_file_bytes"] + 8192,
                                    connection_limit=20, channel_timeout=30)
        self.thread = Thread(target=self.server.run, name="file-web", daemon=True)
        self.thread.start()
        print("File manager listening on loopback port " + str(port) + ".", flush=True)

    def close(self):
        if self.server is not None:
            self.server.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
