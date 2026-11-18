"""Small WSGI file manager, served on loopback behind HTTPS by Waitress."""
from http.cookies import SimpleCookie
from email.utils import formatdate
import hmac
import json
from pathlib import Path
import re
import sys
from threading import Thread
from urllib.parse import parse_qs, quote, urlsplit

from file_preview import read_office
from file_store import FileConflict, FileError, FileStore, SESSION_SECONDS


ASSETS = Path(__file__).resolve().parent / "file_web_assets"


class FileBody:
    def __init__(self, path, length, offset=0):
        self.path, self.length, self.offset = path, length, offset
        self.source = path.open("rb")

    def __iter__(self):
        with self.source as source:
            source.seek(self.offset)
            remaining = self.length
            while remaining:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    raise OSError("Stored file ended before the requested length")
                remaining -= len(chunk)
                yield chunk

    def close(self):
        self.source.close()


def requested_range(header, size):
    """One byte range; unsupported or malformed Range headers use a full response."""
    if len(header) > 200:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()):
        return None
    start, end = match.groups()
    if not start:
        suffix = int(end)
        if not suffix or not size:
            raise ValueError("Unsatisfiable byte range")
        return max(0, size - suffix), size - 1
    start = int(start)
    end = min(int(end), size - 1) if end else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable byte range")
    return start, end


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
                   ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
        try:
            status, content_type, body, extra = self.dispatch(env)
            if content_type == "application/pdf":
                headers = [(key, value) for key, value in headers if key not in {"X-Frame-Options", "Content-Security-Policy"}]
                headers += [("X-Frame-Options", "SAMEORIGIN"), ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'self'")]
            headers += extra
        except FileConflict as error:
            status, content_type = "409 Conflict", "application/json; charset=utf-8"
            body = json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8")
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
            if isinstance(body, FileBody):
                body.close()
            return [b""]
        return body if isinstance(body, FileBody) else [body]

    def dispatch(self, env):
        if not self.store.enabled():
            raise FileError("Сховище вимкнене.")
        origin = public_origin(self.store.config())
        method, path = env.get("REQUEST_METHOD", "GET"), env.get("PATH_INFO", "/")
        if env.get("HTTP_HOST", "") != urlsplit(origin).netloc:
            raise FileError("Невідома адреса сайта.")
        if method in {"GET", "HEAD"} and path in {"/", "/app.css", "/app.js", "/folder-upload.js", "/document-preview.js", "/folder-tree.js"}:
            filename, mime = {"/": ("index.html", "text/html; charset=utf-8"),
                              "/app.css": ("app.css", "text/css; charset=utf-8"),
                              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                              "/folder-upload.js": ("folder-upload.js", "text/javascript; charset=utf-8"),
                              "/document-preview.js": ("document-preview.js", "text/javascript; charset=utf-8"),
                              "/folder-tree.js": ("folder-tree.js", "text/javascript; charset=utf-8")}[path]
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
        if method == "POST" and path == "/api/folders/import":
            body = self.read_json(env, 16 * 1024 * 1024)
            result = self.store.import_folders(user_id, body.get("directories"), body.get("files"), body.get("parent_id"))
            return self.json_response(result)
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
        preview = re.fullmatch(r"/api/files/([1-9][0-9]*)/(preview|content)", path)
        if preview and method in {"GET", "POST"}:
            file_id, action = int(preview[1]), preview[2]
            if action == "content" and method == "POST":
                body = self.read_json(env, 6 * 1024 * 1024 + 8192)
                return self.json_response(self.store.replace_text(user_id, file_id, body.get("text"), body.get("revision")))
            if action == "preview" and method == "GET":
                file_path, name, size = self.store.get_file(user_id, file_id)
                suffix = Path(name).suffix.casefold()
                if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
                    return self.json_response(self.store.text_preview(user_id, file_id))
                if suffix == ".pdf":
                    return self.json_response({"kind": "pdf", "name": name, "url": "/api/files/" + str(file_id) + "/preview.pdf"})
                if suffix in {".docx", ".xlsx"}:
                    result = read_office(file_path, suffix)
                    self.store.require_user(user_id)
                    return self.json_response(dict(result, name=name, editable=False))
                raise FileError("Перегляд підтримує DOCX, XLSX, PDF, TXT, MD, CSV, JSON і LOG. Старі DOC/XLS скачай або збережи як DOCX/XLSX.")
        match = re.fullmatch(r"/api/files/([1-9][0-9]*)/(download|preview\.pdf)", path)
        if method in {"GET", "HEAD"} and match:
            file_path, name, size = self.store.get_file(user_id, int(match[1]))
            inline_pdf = match[2] == "preview.pdf"
            if inline_pdf:
                if Path(name).suffix.casefold() != ".pdf":
                    raise FileError("Це не PDF-файл.")
                with file_path.open("rb") as source:
                    if not source.read(1024).lstrip().startswith(b"%PDF-"):
                        raise FileError("Файл не містить коректного заголовка PDF.")
            mime = "application/pdf" if inline_pdf else "application/octet-stream"
            disposition = ("inline" if inline_pdf else "attachment") + "; filename=download; filename*=UTF-8''" + quote(name, safe="")
            modified = formatdate(file_path.stat().st_mtime, usegmt=True)
            etag = '"' + file_path.name + '"'
            headers = [("Content-Disposition", disposition), ("Accept-Ranges", "bytes"),
                       ("ETag", etag), ("Last-Modified", modified)]
            byte_range = None
            if method == "GET" and env.get("HTTP_RANGE") and env.get("HTTP_IF_RANGE", etag) in {etag, modified}:
                try:
                    byte_range = requested_range(env["HTTP_RANGE"], size)
                except ValueError:
                    return ("416 Range Not Satisfiable", "application/json", b'{"error":"Invalid byte range"}',
                            headers + [("Content-Range", "bytes */" + str(size))])
            if byte_range:
                start, end = byte_range
                return ("206 Partial Content", mime, FileBody(file_path, end - start + 1, start),
                        headers + [("Content-Range", "bytes " + str(start) + "-" + str(end) + "/" + str(size))])
            return "200 OK", mime, FileBody(file_path, size), headers
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
                                    max_request_body_size=max(self.store.limits()["max_file_bytes"], 16 * 1024 * 1024) + 8192,
                                    connection_limit=20, channel_timeout=30)
        self.thread = Thread(target=self.server.run, name="file-web", daemon=True)
        self.thread.start()
        print("File manager listening on loopback port " + str(port) + ".", flush=True)

    def close(self):
        if self.server is not None:
            self.server.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
