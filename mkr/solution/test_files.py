from collections import OrderedDict
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
from zipfile import ZipFile

from file_store import FileStore, FileError, password_hash, password_valid, fingerprint
from file_web import FileApp, FileWeb, public_origin
import file_questions as questions
import telegram_bot as bot


class FileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "test-password-1234"
        cls.hash = password_hash(cls.password)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"FILES_ENABLED": "1", "FILES_ALLOWED_USER_IDS": "101, 202",
                       "FILES_PASSWORD_HASH": self.hash, "FILES_PUBLIC_URL": "https://files.example.org"}
        self.store = FileStore(Path(self.temp.name), config=lambda: self.config)
        self.app = FileApp(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path, method="GET", body=b"", token="", csrf="", origin="https://files.example.org", content_type="application/json", headers=None):
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        route, _, query = path.partition("?")
        env = {"REQUEST_METHOD": method, "PATH_INFO": route, "QUERY_STRING": query,
               "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": content_type,
               "wsgi.input": BytesIO(body), "HTTP_HOST": "files.example.org",
               "HTTP_ORIGIN": origin, "HTTP_COOKIE": "files_session=" + token,
               "HTTP_X_CSRF_TOKEN": csrf}
        env.update(headers or {})
        captured = {}
        def start(status, headers):
            captured.update(status=status, headers=dict(headers))
        captured["body"] = b"".join(self.app(env, start))
        return captured

    def session(self, user_id=101):
        grant = self.store.issue_grant(user_id)
        return self.store.login(grant, self.password)

    def test_password_hash_is_salted_and_weak_password_rejected(self):
        self.assertNotIn(self.password, self.hash)
        self.assertTrue(password_valid(self.password, self.hash))
        self.assertFalse(password_valid("wrong", self.hash))
        self.assertFalse(password_valid("anything", "broken"))
        with self.assertRaises(FileError):
            password_hash("1234")

    def test_grant_requires_actual_allowed_user_id_and_is_single_use(self):
        for user in (True, 0, -1, 303, "101", None):
            with self.assertRaises(FileError):
                self.store.issue_grant(user)
        grant = self.store.issue_grant(101)
        db = self.store.connect()
        try:
            self.assertEqual(db.execute("SELECT hash FROM grants").fetchone()[0], fingerprint(grant))
        finally:
            db.close()
        with self.assertRaises(FileError):
            self.store.login(grant, "wrong")
        token, csrf = self.store.login(grant, self.password)
        self.assertEqual(self.store.session(token), (101, csrf))
        with self.assertRaises(FileError):
            self.store.login(grant, self.password)

    def test_expired_grant_and_session_and_logout(self):
        grant = self.store.issue_grant(101)
        with patch("file_store.time.time", return_value=time.time() + 301):
            with self.assertRaises(FileError):
                self.store.login(grant, self.password)
        token, csrf = self.session()
        with patch("file_store.time.time", return_value=time.time() + 7201):
            with self.assertRaises(FileError):
                self.store.session(token)
        token, csrf = self.session()
        self.store.logout(token)
        with self.assertRaises(FileError):
            self.store.session(token)

    def test_allowlist_revocation_closes_existing_session_and_ai_access(self):
        token, csrf = self.session()
        file_id = self.store.upload(101, "text.txt", b"private data")
        self.config["FILES_ALLOWED_USER_IDS"] = "202"
        with self.assertRaises(FileError):
            self.store.session(token)
        with patch.object(questions, "read_document") as extract, patch.object(questions, "chat") as model:
            with self.assertRaises(FileError):
                questions.answer_file(self.store, 101, file_id, "read", "model")
        extract.assert_not_called()
        model.assert_not_called()
        self.assertEqual(self.request('/api/files', token=token)["status"], "403 Forbidden")

    def test_password_rotation_closes_existing_session(self):
        token, _ = self.session()
        self.config["FILES_PASSWORD_HASH"] = "different-hash"
        with self.assertRaises(FileError):
            self.store.session(token)

    def test_upload_uses_generated_blob_paths_and_shared_access(self):
        file_id = self.store.upload(101, "../../чужий.txt", "Привіт 🌍".encode("utf-8"))
        path, name, size = self.store.get_file(202, file_id)
        self.assertEqual(name, "чужий.txt")
        self.assertEqual(path.parent, self.store.blobs)
        self.assertRegex(path.name, r"^[0-9a-f]{48}$")
        self.assertEqual(path.read_bytes().decode("utf-8"), "Привіт 🌍")
        self.assertEqual(self.store.list_files(202)[0]["id"], file_id)
        for user in (303, None):
            with self.assertRaises(FileError):
                self.store.get_file(user, file_id)

    def test_invalid_large_and_over_quota_uploads_leave_no_orphan_files(self):
        for name, data in (("", b"a"), ("..", b"a"), ("a\n.txt", b"a")):
            with self.assertRaises(FileError):
                self.store.upload(101, name, data)
        with patch("file_store.MAX_FILE_BYTES", 2):
            with self.assertRaises(FileError):
                self.store.upload(101, "large", b"abc")
        with patch("file_store.MAX_STORAGE_BYTES", 2):
            with self.assertRaises(FileError):
                self.store.upload(101, "large", b"abc")
        self.assertEqual(list(self.store.blobs.iterdir()), [])

    def test_folder_import_uploads_nested_and_empty_files_into_selected_destination(self):
        parent = self.store.create_folder(101, "Destination")
        token, csrf = self.session()
        response = self.request('/api/folders/import', 'POST', {
            "directories": ["Root", "Root/Nested", "Root/Empty"],
            "files": [{"path": "Root/Nested/notes.txt", "size": 5}, {"path": "Root/zero", "size": 0}],
            "parent_id": parent}, token, csrf)
        self.assertEqual(response["status"], "200 OK")
        mapping = json.loads(response["body"])["folders"]
        for name, data, folder in (("notes.txt", b"hello", "Root/Nested"), ("zero", b"", "Root")):
            uploaded = self.request('/api/files?name=' + name + '&folder=' + str(mapping[folder]),
                                    'POST', data, token, csrf, content_type='application/octet-stream')
            self.assertEqual(uploaded["status"], "200 OK")
            file_id = json.loads(uploaded["body"])["id"]
            self.assertEqual(self.store.get_file(202, file_id)[0].read_bytes(), data)
        folders = {item["id"]: item for item in self.store.list_folders(101)}
        self.assertEqual(folders[mapping["Root"]]["parent_id"], parent)
        self.assertEqual(folders[mapping["Root/Empty"]]["parent_id"], mapping["Root"])

    def test_folder_import_does_not_merge_or_overwrite_existing_roots(self):
        old = self.store.create_folder(101, "Root")
        file_id = self.store.upload(101, "old.txt", b"old", old)
        result = self.store.import_folders(101, ["Root/Child"], [])
        self.assertEqual(result["roots"][0]["name"], "Root (2)")
        self.assertNotEqual(result["folders"]["Root"], old)
        self.assertEqual(self.store.get_file(101, file_id)[0].read_bytes(), b"old")
        self.assertEqual(self.store.import_folders(101, ["Root"], [])["roots"][0]["name"], "Root (3)")

    def test_folder_manifest_validation_precedes_any_mutation(self):
        for directories, files in [
            (["../outside"], []), (["/root"], []), (["C:/root"], []), (["a\\b"], []),
            (["a/./b"], []), (["a//b"], []), (["a/.. /b"], []),
            (["Root/Sub", "Root/sub"], []), (["/".join(["x"] * 33)], []),
            (["Root"], [{"path":"Root/a", "size":-1}]),
            (["Root"], [{"path":"Root/a", "size":True}]),
            (["Root"], [{"path":"../x", "size":1}]),
            (["Root"], [{"path":"Root/a", "size":1}] * 2),
            (["Root/a"], [{"path":"Root/a", "size":1}]),
            ([], []), (["Root"] * 10001, [])]:
            with self.subTest(directories=directories[:2]):
                with self.assertRaises(FileError):
                    self.store.import_folders(101, directories, files)
                self.assertEqual(self.store.list_folders(101), [])
        with patch('file_store.MAX_FILE_BYTES', 2):
            with self.assertRaises(FileError):
                self.store.import_folders(101, ["Root"], [{"path":"Root/a", "size":3}])
        with patch('file_store.MAX_STORAGE_BYTES', 2):
            with self.assertRaises(FileError):
                self.store.import_folders(101, ["Root"], [{"path":"Root/a", "size":2}, {"path":"Root/b", "size":1}])
        with patch('file_store.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaises(FileError):
                self.store.import_folders(101, ["Root"], [])
        self.assertEqual(self.store.list_folders(101), [])

    def test_folder_import_endpoint_rechecks_permissions_and_csrf(self):
        body = {"directories":["Root"], "files":[]}
        self.assertEqual(self.request('/api/folders/import', 'POST', body)["status"], '403 Forbidden')
        token, csrf = self.session()
        self.assertEqual(self.request('/api/folders/import', 'POST', body, token)["status"], '403 Forbidden')
        self.assertEqual(self.request('/api/folders/import', 'POST', body, token, csrf, 'https://evil.example')["status"], '403 Forbidden')
        self.config["FILES_ALLOWED_USER_IDS"] = "202"
        self.assertEqual(self.request('/api/folders/import', 'POST', body, token, csrf)["status"], '403 Forbidden')
        self.assertEqual(self.store.list_folders(202), [])
        asset = self.request('/folder-upload.js')
        self.assertEqual(asset["status"], '200 OK')
        self.assertIn(b'fromEntries', asset["body"])

    def test_folder_import_depth_counts_destination_and_rolls_back(self):
        parent = None
        for index in range(31):
            parent = self.store.create_folder(101, str(index), parent)
        with self.assertRaises(FileError):
            self.store.import_folders(101, ["Root/Child"], [], parent)
        self.assertEqual(len(self.store.list_folders(101)), 31)

    def test_login_requires_nonce_password_and_origin_and_sets_secure_cookie(self):
        grant = self.store.issue_grant(101)
        denied = self.request('/api/login', 'POST', {"code": grant, "password": self.password}, origin="https://evil.example")
        self.assertEqual(denied["status"], "403 Forbidden")
        bad = self.request('/api/login', 'POST', {"code": "101", "password": self.password})
        self.assertEqual(bad["status"], "403 Forbidden")
        response = self.request('/api/login', 'POST', {"code": grant, "password": self.password})
        self.assertEqual(response["status"], "200 OK")
        cookie = response["headers"]["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)

    def test_unauthorized_list_and_download_and_csrf_upload_are_rejected(self):
        file_id = self.store.upload(101, "notes.txt", b"secret text")
        self.assertEqual(self.request('/api/files')["status"], "403 Forbidden")
        self.assertEqual(self.request('/api/files/' + str(file_id) + '/download')["status"], "403 Forbidden")
        token, csrf = self.session()
        for wrong_csrf, origin in (("", self.config["FILES_PUBLIC_URL"]), (csrf, "https://evil.example")):
            result = self.request('/api/files?name=new.txt', 'POST', b"new data", token, wrong_csrf, origin, 'application/octet-stream')
            self.assertEqual(result["status"], "403 Forbidden")
        self.assertEqual(len(self.store.list_files(101)), 1)

    def test_web_upload_listing_and_forced_attachment_keep_filenames_as_data(self):
        token, csrf = self.session()
        name = '<script>alert(1)</script>.txt'
        response = self.request('/api/files?name=' + quote(name, safe=""), 'POST', b"hello", token, csrf, content_type='application/octet-stream')
        self.assertEqual(response["status"], "200 OK")
        file_id = json.loads(response["body"])["id"]
        result = self.request('/api/files', token=token)
        self.assertEqual(result["headers"]["Content-Type"], "application/json; charset=utf-8")
        result = self.request('/api/files/' + str(file_id) + '/download', token=token)
        self.assertEqual(result["body"], b"hello")
        self.assertTrue(result["headers"]["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(result["headers"]["X-Content-Type-Options"], "nosniff")
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")

    def test_static_login_has_csp_and_leaks_no_files_or_grant(self):
        self.store.upload(101, "hidden-name.txt", b"private")
        result = self.request('/')
        self.assertEqual(result["status"], "200 OK")
        self.assertNotIn(b"hidden-name.txt", result["body"])
        self.assertIn("frame-ancestors 'none'", result["headers"]["Content-Security-Policy"])
        self.assertEqual(result["headers"]["Referrer-Policy"], "no-referrer")

    def test_login_attempts_rate_limited_and_disabled_web_stays_closed(self):
        db = self.store.connect()
        try:
            with db:
                db.executemany("INSERT INTO login_failures VALUES (?)", [(time.time(),)] * 20)
        finally:
            db.close()
        with self.assertRaisesRegex(FileError, "Забагато"):
            self.store.login("missing", self.password)
        self.config["FILES_ENABLED"] = "0"
        self.assertEqual(self.request('/')["status"], "403 Forbidden")

    def test_public_address_rejects_plain_http_except_loopback(self):
        for address in ('http://files.example.org', 'https://user:pass@files.example.org', 'https://files.example.org/subpath', 'https://files.example.org?x=1'):
            with self.assertRaises(FileError):
                public_origin({"FILES_PUBLIC_URL": address})
        self.assertEqual(public_origin({"FILES_PUBLIC_URL": "http://127.0.0.1:8080"}), "http://127.0.0.1:8080")

    def test_document_text_is_bounded_and_isolated_from_conversation_history(self):
        file_id = self.store.upload(101, "notes.txt", "Зміст документа 🌍".encode("utf-8"))
        with patch.object(questions, "chat", return_value={"message": {"content": "Висновок"}, "done_reason": "stop"}) as model:
            answer = questions.answer_file(self.store, 101, file_id, "Що тут?", "model")
        self.assertEqual(answer, "Висновок")
        messages = model.call_args.args[0]
        self.assertEqual(len(messages), 2)
        value = json.loads(messages[1]["content"])
        self.assertEqual(value["file_content"], "Зміст документа 🌍")
        self.assertNotIn(str(self.store.root), json.dumps(messages))
        self.assertNotIn(self.password, json.dumps(messages))

    def test_docx_is_extracted_without_running_any_embedded_code(self):
        buffer = BytesIO()
        with ZipFile(buffer, "w") as doc:
            doc.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Тестовий документ</w:t></w:r></w:p></w:body></w:document>')
        file_id = self.store.upload(101, "sample.docx", buffer.getvalue())
        path, _, _ = self.store.get_file(101, file_id)
        self.assertIn("Тестовий документ", questions.read_document(path, ".docx"))

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "Optional pypdf is not installed")
    def test_pdf_text_extraction_with_real_reader(self):
        stream = b"BT /F1 12 Tf 72 720 Td (Hello PDF fixture) Tj ET"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
                   b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        data, offsets = bytearray(b"%PDF-1.4\n"), [0]
        for number, value in enumerate(objects, 1):
            offsets.append(len(data))
            data.extend(str(number).encode() + b" 0 obj\n" + value + b"\nendobj\n")
        start = len(data)
        data.extend(b"xref\n0 6\n0000000000 65535 f \n")
        for offset in offsets[1:]:
            data.extend(("%010d 00000 n \n" % offset).encode())
        data.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF")
        file_id = self.store.upload(101, "sample.pdf", data)
        path, _, _ = self.store.get_file(101, file_id)
        self.assertIn("Hello PDF fixture", questions.read_document(path, ".pdf"))

    def test_large_document_uses_relevant_excerpts_with_explicit_notice(self):
        text = "Загальна частина. " * 1000 + "\nВимкнення електрики завтра о 15:00.\n" + "Додатки. " * 1000
        content, partial = questions.excerpt(text, "Коли вимкнення електрики?")
        self.assertTrue(partial)
        self.assertLessEqual(len(content), 5000)
        self.assertIn("15:00", content)

    def test_unsupported_files_are_stored_but_not_sent_to_model(self):
        file_id = self.store.upload(101, "archive.zip", b"not executable")
        with patch.object(questions, "chat") as model:
            with self.assertRaisesRegex(FileError, "Цей формат"):
                questions.answer_file(self.store, 101, file_id, "read", "model")
        model.assert_not_called()

    def test_private_id_command_and_denied_file_command(self):
        api = Mock()
        message = {"chat": {"id": 999, "type": "private"}, "from": {"id": 303}, "text": "/id"}
        bot.handle_update(api, {"update_id": 1, "message": message}, None, OrderedDict(), "model")
        self.assertIn("303", api.send.call_args.args[1])
        self.assertNotIn("999", api.send.call_args.args[1])
        message["text"] = "/file 1 read"
        with patch.object(questions, "FileStore", return_value=self.store), patch.object(questions, "answer_file") as answer:
            bot.handle_update(api, {"update_id": 2, "message": message}, None, OrderedDict(), "model")
        answer.assert_not_called()
        self.assertIn("не дозволений", api.send.call_args.args[1])

    def test_bot_lists_files_and_issues_fragment_link_and_does_not_keep_file_history(self):
        file_id = self.store.upload(101, "notes.txt", b"hello")
        api, history = Mock(), OrderedDict()
        message = {"chat": {"id": 999, "type": "private"}, "from": {"id": 101}, "text": "/files"}
        with patch.object(questions, "FileStore", return_value=self.store):
            bot.handle_update(api, {"update_id": 1, "message": message}, None, history, "model")
        self.assertIn('/#access=', api.send.call_args.args[1])
        self.assertIn('notes.txt', api.send.call_args.args[1])
        message["text"] = "/file " + str(file_id) + " поясни"
        with patch.object(questions, "FileStore", return_value=self.store), patch.object(questions, "answer_file", return_value="**Result** <unsafe>"):
            bot.handle_update(api, {"update_id": 2, "message": message}, None, history, "model")
        self.assertEqual(api.send.call_args.args[1], "<b>Result</b> &lt;unsafe&gt;")
        self.assertEqual(history, {})

    def test_file_web_disabled_does_not_import_optional_server(self):
        self.config["FILES_ENABLED"] = "0"
        web = FileWeb(self.store)
        web.start()
        self.assertIsNone(web.server)
        web.close()

    def test_file_web_binds_only_loopback_and_closes_server(self):
        server = Mock()
        create = Mock(return_value=server)
        web = FileWeb(self.store)
        with patch.dict('sys.modules', {"waitress": SimpleNamespace(create_server=create)}):
            web.start()
            web.close()
        self.assertEqual(create.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(create.call_args.kwargs["port"], 8080)
        self.assertEqual(create.call_args.kwargs["threads"], 2)
        server.close.assert_called_once()

    def test_configurable_limits_and_real_storage_usage(self):
        self.config.update(FILES_STORAGE_LIMIT_MB="2", FILES_MAX_FILE_MB="1")
        self.assertEqual(self.store.limits()["limit_bytes"], 2 * 1024 * 1024)
        self.assertEqual(self.store.limits()["max_file_bytes"], 1024 * 1024)
        self.store.upload(101, "one.txt", b"hello")
        info = self.store.storage_info(101)
        self.assertEqual(info["used_bytes"], 5)
        self.assertEqual(info["available_bytes"], 2 * 1024 * 1024 - 5)
        with self.assertRaises(FileError):
            self.store.upload(101, "big", b"x" * (1024 * 1024 + 1))
        for value in ("0", "bad", "-5", "16385"):
            self.config["FILES_MAX_FILE_MB"] = value
            with self.assertRaises(FileError):
                self.store.limits()

    def test_streaming_upload_checks_incomplete_data_and_disk_space(self):
        with self.assertRaises(FileError):
            self.store.upload_stream(101, "broken.txt", BytesIO(b"short"), 10)
        self.assertEqual(self.store.list_files(101), [])
        self.assertEqual(list(self.store.blobs.iterdir()), [])
        with patch("file_store.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(FileError, "диску"):
                self.store.upload(101, "file.txt", b"hello")

    def test_existing_files_can_be_downloaded_after_lowering_upload_limit(self):
        file_id = self.store.upload(101, "data.txt", b"test content")
        token, _ = self.session()
        with patch('file_store.MAX_FILE_BYTES', 2), patch.object(Path, "read_bytes", side_effect=AssertionError("download must stream")):
            result = self.request('/api/files/' + str(file_id) + '/download', token=token)
        self.assertEqual(result["body"], b"test content")
        self.assertEqual(result["headers"]["Content-Length"], "12")

    def test_folder_creation_upload_move_and_rename_preserve_file_id(self):
        folder = self.store.create_folder(101, "Документи")
        child = self.store.create_folder(202, "Навчання", folder)
        file_id = self.store.upload(101, "notes.txt", b"hello", child)
        self.assertEqual(self.store.list_files(202)[0]["folder_id"], child)
        self.store.update_item(202, "file", file_id, "renamed.txt", folder)
        self.assertEqual(self.store.get_file(101, file_id)[1], "renamed.txt")
        self.assertEqual(self.store.list_files(101)[0]["folder_id"], folder)
        self.store.update_item(101, "folder", child, "Університет", None)
        self.assertEqual(next(item for item in self.store.list_folders(101) if item["id"] == child)["name"], "Університет")

    def test_folder_cycles_duplicate_names_invalid_destinations_and_access_denied(self):
        folder = self.store.create_folder(101, "Папка")
        child = self.store.create_folder(101, "Підпапка", folder)
        with self.assertRaises(FileError):
            self.store.create_folder(101, "пАПКА")
        for destination in (folder, child):
            with self.assertRaises(FileError):
                self.store.update_item(101, "folder", folder, "Папка", destination)
        with self.assertRaises(FileError):
            self.store.upload(101, "bad.txt", b"hello", 99999)
        with self.assertRaises(FileError):
            self.store.create_folder(101, " .. ")
        with self.assertRaises(FileError):
            self.store.create_folder(303, "Denied")

    def test_folder_web_endpoints_require_csrf_and_return_updated_listing(self):
        token, csrf = self.session()
        denied = self.request('/api/folders', 'POST', {"name":"Нова", "parent_id":None}, token)
        self.assertEqual(denied["status"], "403 Forbidden")
        result = self.request('/api/folders', 'POST', {"name":"Нова", "parent_id":None}, token, csrf)
        folder = json.loads(result["body"])["id"]
        upload = self.request('/api/files?name=test.txt&folder=' + str(folder), 'POST', b"hello", token, csrf, content_type='application/octet-stream')
        file_id = json.loads(upload["body"])["id"]
        result = self.request('/api/files/' + str(file_id), 'POST', {"name":"renamed.txt", "parent_id":None}, token, csrf)
        self.assertEqual(result["status"], "200 OK")
        listing = json.loads(self.request('/api/files', token=token)["body"])
        self.assertEqual(listing["files"][0]["name"], "renamed.txt")
        self.assertIsNone(listing["files"][0]["folder_id"])
        self.assertEqual(listing["folders"][0]["id"], folder)
        self.assertEqual(listing["storage"]["used_bytes"], 5)

    def test_old_file_database_migrates_into_root_folder_without_losing_ids(self):
        db = sqlite3.connect(str(self.store.root / 'files.sqlite3'))
        db.execute('CREATE TABLE files (id INTEGER PRIMARY KEY,blob TEXT NOT NULL UNIQUE,name TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,uploaded_by INTEGER NOT NULL,created_at REAL NOT NULL)')
        db.execute('INSERT INTO files VALUES (7,?,?,?,?,?,?)', ('a' * 48, 'legacy.txt', 5, 'hash', 101, time.time()))
        db.commit()
        db.close()
        self.store.blobs.mkdir()
        (self.store.blobs / ('a' * 48)).write_bytes(b'hello')
        item = self.store.list_files(101)[0]
        self.assertEqual(item["id"], 7)
        self.assertIsNone(item["folder_id"])
        self.assertEqual(self.store.get_file(202, 7)[0].read_bytes(), b'hello')

    def test_large_file_limit_accepts_one_and_half_gib_and_upload_stays_chunked(self):
        length = 1536 * 1024 * 1024
        self.assertGreaterEqual(self.store.limits()["max_file_bytes"], length)
        class VirtualStream:
            remaining = length
            largest = 0
            block = b'x' * (1024 * 1024)
            def read(self, count):
                self.largest = max(self.largest, count)
                amount = min(count, self.remaining)
                self.remaining -= amount
                return self.block[:amount]
        class CountingOutput:
            total = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def write(self, data): self.total += len(data)
        stream, output = VirtualStream(), CountingOutput()
        digest = SimpleNamespace(update=Mock(), hexdigest=lambda: '0' * 64)
        with patch.object(Path, 'open', return_value=output), patch('file_store.hashlib.sha256', return_value=digest), patch('file_store.shutil.disk_usage', return_value=SimpleNamespace(free=4 * length)):
            file_id = self.store.upload_stream(101, 'archive.bin', stream, length)
        self.assertEqual(stream.remaining, 0)
        self.assertLessEqual(stream.largest, 1024 * 1024)
        self.assertEqual(output.total, length)
        self.assertEqual(self.store.list_files(101)[0]['size'], length)
        self.assertEqual(self.store.list_files(101)[0]['id'], file_id)

    def test_download_supports_open_closed_and_suffix_ranges(self):
        file_id = self.store.upload(101, 'data.bin', b'0123456789abcdefghij')
        token, _ = self.session()
        for header, expected, content_range in [('bytes=3-7', b'34567', 'bytes 3-7/20'),
                                                ('bytes=15-', b'fghij', 'bytes 15-19/20'),
                                                ('bytes=-4', b'ghij', 'bytes 16-19/20'),
                                                ('bytes=18-999', b'ij', 'bytes 18-19/20')]:
            result = self.request('/api/files/' + str(file_id) + '/download', token=token, headers={'HTTP_RANGE':header})
            self.assertEqual(result['status'], '206 Partial Content')
            self.assertEqual(result['body'], expected)
            self.assertEqual(result['headers']['Content-Range'], content_range)
            self.assertEqual(result['headers']['Content-Length'], str(len(expected)))
            self.assertEqual(result['headers']['Accept-Ranges'], 'bytes')

    def test_range_requires_auth_and_checks_file_identity_before_resuming(self):
        file_id = self.store.upload(101, 'data.bin', b'abcdefghij')
        path = '/api/files/' + str(file_id) + '/download'
        denied = self.request(path, headers={'HTTP_RANGE':'bytes=4-'})
        self.assertEqual(denied['status'], '403 Forbidden')
        self.assertNotIn('Content-Range', denied['headers'])
        token, _ = self.session()
        etag = self.request(path, token=token)['headers']['ETag']
        result = self.request(path, token=token, headers={'HTTP_RANGE':'bytes=4-', 'HTTP_IF_RANGE':etag})
        self.assertEqual(result['body'], b'efghij')
        result = self.request(path, token=token, headers={'HTTP_RANGE':'bytes=4-', 'HTTP_IF_RANGE':'"different-version"'})
        self.assertEqual(result['status'], '200 OK')
        self.assertEqual(result['body'], b'abcdefghij')

    def test_unsatisfiable_range_and_head_and_unsupported_ranges(self):
        file_id = self.store.upload(101, 'data.bin', b'abcdefghij')
        path = '/api/files/' + str(file_id) + '/download'
        token, _ = self.session()
        for header in ('bytes=10-', 'bytes=8-3', 'bytes=-0'):
            result = self.request(path, token=token, headers={'HTTP_RANGE':header})
            self.assertEqual(result['status'], '416 Range Not Satisfiable')
            self.assertEqual(result['headers']['Content-Range'], 'bytes */10')
        result = self.request(path, method='HEAD', token=token, headers={'HTTP_RANGE':'bytes=2-4'})
        self.assertEqual(result['status'], '200 OK')
        self.assertEqual(result['body'], b'')
        self.assertEqual(result['headers']['Content-Length'], '10')
        for header in ('items=0-1', 'bytes=0-1,3-4', 'bytes=invalid'):
            result = self.request(path, token=token, headers={'HTTP_RANGE':header})
            self.assertEqual(result['status'], '200 OK')
            self.assertEqual(result['body'], b'abcdefghij')


if __name__ == "__main__":
    unittest.main()
