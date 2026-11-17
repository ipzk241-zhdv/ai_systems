from html import escape
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import io

import llm
from inline_bot import InlineAnswers, trim_text
from message_worker import PrivateMessages
from telegram_bot import format_chat_html, open_state, poll


class API:
    def __init__(self):
        self.calls = []
        self.event = threading.Event()

    def call(self, method, **params):
        self.calls.append((method, params))
        self.event.set()

    def send(self, *args, **kwargs):
        pass


def query(text, ident="1", user=123):
    return {"id": ident, "from": {"id": user}, "query": text, "offset": ""}


class InlineTest(unittest.TestCase):
    def test_empty_and_long_queries_do_not_generate(self):
        api = API()
        generate = unittest.mock.Mock()
        inline = InlineAnswers(api, "model", escape, generate=generate)
        inline.submit(query(""))
        inline.submit(query("x" * 401))
        generate.assert_not_called()
        self.assertEqual(len(inline.pending), 0)
        self.assertTrue(all(call[1]["is_personal"] for call in api.calls))
        self.assertTrue(all(call[1]["results"] == [] for call in api.calls))

    def test_typing_is_debounced_and_answer_is_escaped(self):
        api = API()
        generated = []
        def generate(prompt, model, timeout):
            generated.append(prompt)
            return "**Answer** <script>"
        inline = InlineAnswers(api, "model", format_chat_html, generate=generate, debounce=.04)
        inline.start()
        try:
            inline.submit(query("explain", "1"))
            inline.submit(query("explain an OCPU", "2"))
            self.assertTrue(api.event.wait(2))
            self.assertEqual(generated, ["explain an OCPU"])
            result = api.calls[-1][1]
            self.assertEqual(result["inline_query_id"], "2")
            self.assertEqual(result["results"][0]["input_message_content"]["message_text"], "<b>Answer</b> &lt;script&gt;")
            inline.submit(query("explain an OCPU", "3"))
            self.assertEqual(generated, ["explain an OCPU"])
            self.assertEqual(api.calls[-1][1]["inline_query_id"], "3")
            inline.submit(query("explain an OCPU", "4", user=456))
            self.assertIn(456, inline.pending)
        finally:
            inline.close()

    def test_outdated_result_is_not_sent(self):
        api = API()
        entered, release = threading.Event(), threading.Event()
        def generate(prompt, model, timeout):
            if prompt == "first":
                entered.set()
                release.wait(2)
            return prompt
        inline = InlineAnswers(api, "model", escape, generate=generate, debounce=0)
        inline.start()
        try:
            inline.submit(query("first", "old"))
            self.assertTrue(entered.wait(1))
            inline.submit(query("second", "new"))
            release.set()
            self.assertTrue(api.event.wait(2))
            self.assertEqual([params["inline_query_id"] for _, params in api.calls], ["new"])
        finally:
            release.set()
            inline.close()

    def test_failed_generation_returns_private_chat_button(self):
        api = API()
        def generate(*args): raise ValueError("secret detail")
        inline = InlineAnswers(api, "model", escape, generate=generate, debounce=0)
        inline.start()
        try:
            inline.submit(query("hello"))
            self.assertTrue(api.event.wait(2))
            self.assertEqual(api.calls[-1][1]["results"], [])
            self.assertIn("button", api.calls[-1][1])
            self.assertNotIn("secret detail", str(api.calls))
        finally:
            inline.close()

    def test_unicode_answer_fits_telegram(self):
        text = trim_text("🙂" * 4000)
        self.assertLessEqual(len(text.encode("utf-16-le")), 6800)
        self.assertTrue(text.endswith("…"))

    def test_fast_model_request_does_not_sleep_or_retry(self):
        llm._remaining_tokens = None
        failure = HTTPError("https://test", 429, "limited", {"Retry-After": "30"}, io.BytesIO())
        with patch.object(llm, "read_config", return_value={"LLM_PROVIDER": "groq", "GROQ_API_KEY": "test"}), patch.object(llm, "urlopen", side_effect=failure) as request, patch.object(llm, "sleep") as wait:
            with self.assertRaises(llm.LLMError):
                llm.chat([], "openai/gpt-oss-20b", options={}, timeout=5, fast=True)
            self.assertEqual(request.call_count, 1)
            wait.assert_not_called()

    def test_fast_request_rejects_quota_wait(self):
        llm._remaining_tokens, llm._reset_at = 0, time.monotonic() + 60
        try:
            with patch.object(llm, "sleep") as wait:
                with self.assertRaises(llm.LLMError):
                    llm.wait_for_quota({"messages": [], "max_completion_tokens": 2048}, fast=True)
                wait.assert_not_called()
        finally:
            llm._remaining_tokens = None

    def test_private_worker_owns_sqlite_and_keeps_order(self):
        api = API()
        done = threading.Event()
        handled = []
        def handler(api, update, db, conversations, model):
            db.execute("SELECT * FROM checks").fetchall()
            handled.append(update["update_id"])
            if len(handled) == 2: done.set()
        with tempfile.TemporaryDirectory() as directory:
            worker = PrivateMessages(api, str(Path(directory) / "bot.sqlite3"), "model", handler, open_state)
            worker.start()
            try:
                worker.submit({"update_id": 1})
                worker.submit({"update_id": 2})
                self.assertTrue(done.wait(2))
                worker.check()
                self.assertEqual(handled, [1, 2])
            finally:
                worker.close()

    def test_poll_routes_inline_even_with_private_message_in_same_batch(self):
        class FakeDB:
            def execute(self, *args): return self
            def fetchone(self): return (0, "main", "example.sqlite3")
        update = {"update_id": 2, "inline_query": query("question")}
        api = unittest.mock.Mock()
        api.call.side_effect = [[{"update_id": 1, "message": {}}, update], KeyboardInterrupt()]
        with patch("telegram_bot.NewsCollector"), patch("telegram_bot.PrivateMessages") as messages, patch("telegram_bot.InlineAnswers") as inline, patch("telegram_bot.GuestAnswers") as guests:
            guests.return_value.accepts.return_value = False
            with self.assertRaises(KeyboardInterrupt): poll(api, FakeDB(), "model")
            messages.return_value.submit.assert_called_once()
            inline.return_value.submit.assert_called_once_with(update["inline_query"])
            inline.return_value.close.assert_called_once()
            self.assertIn("inline_query", api.call.call_args_list[0].kwargs["allowed_updates"])


if __name__ == "__main__":
    unittest.main()
