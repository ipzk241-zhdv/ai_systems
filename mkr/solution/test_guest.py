import threading
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from guest_bot import GuestAnswers
from telegram_bot import format_chat_html, poll
import voice_notes


def message(text="@DBresh_bot explain this", kind="private", guest=True):
    result = {"message_id": 7, "from": {"id": 100, "is_bot": False},
              "chat": {"id": 123, "type": kind}, "text": text}
    if guest:
        result["guest_query_id"] = "guest-query-123"
    return result


class GuestTest(unittest.TestCase):
    def worker(self, generate=None):
        api = Mock()
        worker = GuestAnswers(api, "model", format_chat_html,
                              generate=generate or Mock(return_value="**Answer** <unsafe>"))
        return api, worker

    def test_guest_reply_uses_query_id_in_private_and_group_chats(self):
        for kind in ("private", "group", "supergroup"):
            api, worker = self.worker()
            value = message(kind=kind)
            self.assertTrue(worker.accepts(value))
            worker.process(value)
            method, = api.call.call_args.args
            self.assertEqual(method, "answerGuestQuery")
            params = api.call.call_args.kwargs
            self.assertEqual(params["guest_query_id"], value["guest_query_id"])
            self.assertNotIn("chat_id", params)
            content = params["result"]["input_message_content"]
            self.assertEqual(content["message_text"], "<b>Answer</b> &lt;unsafe&gt;")
            api.send.assert_not_called()

    def test_group_requires_own_mention_or_reply_and_ignores_bots(self):
        api, worker = self.worker()
        self.assertFalse(worker.accepts(message("hello everyone", "group", guest=False)))
        self.assertFalse(worker.accepts(message("@some_other_bot question", "group", guest=False)))
        self.assertFalse(worker.accepts(message("@DBresh_bot_fake question", "group", guest=False)))
        self.assertTrue(worker.accepts(message("@dbresh_BOT question", "group", guest=False)))
        reply = message("why?", "group", guest=False)
        reply["reply_to_message"] = {"from": {"is_bot": True, "username": "DBresh_bot"}}
        self.assertTrue(worker.accepts(reply))
        reply["from"]["is_bot"] = True
        self.assertFalse(worker.accepts(reply))

    def test_member_group_reply_quotes_request_and_preserves_topic(self):
        api, worker = self.worker()
        value = message(kind="supergroup", guest=False)
        value["message_thread_id"] = 99
        worker.process(value)
        api.call.assert_not_called()
        self.assertEqual(api.send.call_args.args[0], 123)
        self.assertEqual(api.send.call_args.kwargs["reply_parameters"]["message_id"], 7)
        self.assertEqual(api.send.call_args.kwargs["message_thread_id"], 99)

    def test_only_current_request_and_direct_quote_reach_model(self):
        generate = Mock(return_value="answer")
        api, worker = self.worker(generate)
        value = message()
        value["reply_to_message"] = {"text": "Quoted text", "reply_to_message": {"text": "Do not disclose"}}
        worker.process(value)
        generate.assert_called_once_with("explain this", "Quoted text", "model")
        worker.process(message("Another question"))
        self.assertEqual(generate.call_args.args, ("Another question", "", "model"))

    def test_empty_or_long_request_does_not_use_model(self):
        generate = Mock()
        api, worker = self.worker(generate)
        worker.process(message("@DBresh_bot"))
        worker.process(message("@DBresh_bot " + "x" * 1501))
        generate.assert_not_called()
        self.assertEqual(api.call.call_count, 2)

    def test_generation_failure_returns_one_safe_reply(self):
        api, worker = self.worker(Mock(side_effect=ValueError("private error detail")))
        worker.process(message())
        self.assertEqual(api.call.call_count, 1)
        self.assertNotIn("private error detail", str(api.call.call_args))
        api.send.assert_not_called()

    def test_guest_query_is_not_processed_twice(self):
        api, worker = self.worker()
        worker.submit(message())
        worker.submit(message())
        self.assertEqual(worker.queue.qsize(), 1)

    def test_guest_worker_runs_separately(self):
        done = threading.Event()
        api, worker = self.worker()
        api.call.side_effect = lambda *args, **kwargs: done.set()
        worker.start()
        try:
            worker.submit(message())
            self.assertTrue(done.wait(2))
            self.assertEqual(api.call.call_args.args[0], "answerGuestQuery")
        finally:
            worker.close()

    def voice_reply(self, guest=True):
        value = message("@DBresh_bot", kind="private" if guest else "group", guest=guest)
        value["reply_to_message"] = {"message_id": 6, "voice": {"file_id": "someone-elses-voice", "duration": 6},
                                     "from": {"id": 999, "is_bot": False}}
        return value

    def test_guest_mention_alone_transcribes_quoted_voice_and_records_metrics(self):
        api, worker = self.worker()
        api.download_voice.return_value = (b"ogg", "voice.ogg")
        db = sqlite3.connect(":memory:")
        try:
            voice_notes.initialize(db)
            with patch.object(voice_notes, "read_config", return_value={"GROQ_API_KEY": "fake"}), patch.object(voice_notes, "transcribe", return_value=("**Буквальний** <текст>", "whisper-large-v3-turbo")):
                worker.process(self.voice_reply(), db)
            worker.generate.assert_not_called()
            api.download_voice.assert_called_once_with("someone-elses-voice", voice_notes.MAX_BYTES)
            api.send.assert_not_called()
            api.call.assert_called_once()
            content = api.call.call_args.kwargs["result"]["input_message_content"]["message_text"]
            self.assertEqual(content, "<b>Розшифровка</b>\n**Буквальний** &lt;текст&gt;")
            self.assertEqual(db.execute("SELECT context,word_count FROM voice_runs").fetchone(), ("guest", 2))
        finally:
            db.close()

    def test_member_group_voice_uses_one_reply(self):
        api, worker = self.worker()
        api.download_voice.return_value = (b"ogg", "voice.ogg")
        with patch.object(voice_notes, "read_config", return_value={"GROQ_API_KEY": "fake"}), patch.object(voice_notes, "transcribe", return_value=("Розпізнаний текст", "whisper-large-v3-turbo")):
            worker.process(self.voice_reply(guest=False))
        api.call.assert_not_called()
        api.send.assert_called_once()
        self.assertEqual(api.send.call_args.args[1], "<b>Розшифровка</b>\nРозпізнаний текст")

    def test_long_guest_transcript_is_not_silently_truncated_or_counted(self):
        api, worker = self.worker()
        api.download_voice.return_value = (b"ogg", "voice.ogg")
        db = sqlite3.connect(":memory:")
        try:
            voice_notes.initialize(db)
            with patch.object(voice_notes, "read_config", return_value={"GROQ_API_KEY": "fake"}), patch.object(voice_notes, "transcribe", return_value=("Текст " * 1000, "whisper-large-v3-turbo")):
                worker.process(self.voice_reply(), db)
            api.send.assert_not_called()
            api.call.assert_called_once()
            content = api.call.call_args.kwargs["result"]["input_message_content"]["message_text"]
            self.assertIn("особистий чат", content)
            self.assertEqual(db.execute("SELECT count(*) FROM voice_runs").fetchone()[0], 0)
        finally:
            db.close()

    def test_guest_voice_worker_owns_metrics_connection(self):
        done = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bot.sqlite3")
            api = Mock()
            api.download_voice.return_value = (b"ogg", "voice.ogg")
            api.call.side_effect = lambda *args, **kwargs: done.set()
            worker = GuestAnswers(api, "model", format_chat_html, database_path=path)
            with patch.object(voice_notes, "read_config", return_value={"GROQ_API_KEY": "fake"}), patch.object(voice_notes, "transcribe", return_value=("Текст", "whisper-large-v3-turbo")):
                worker.start()
                try:
                    worker.submit(self.voice_reply())
                    self.assertTrue(done.wait(2))
                finally:
                    worker.close()
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("SELECT context FROM voice_runs").fetchone(), ("guest",))
            finally:
                db.close()

    def test_poll_subscribes_to_guest_and_routes_it_separately(self):
        class DB:
            def execute(self, *args): return self
            def fetchone(self): return (0, "main", "test.sqlite3")
        api = Mock()
        guest = message()
        api.call.side_effect = [[{"update_id": 1, "guest_message": guest}], KeyboardInterrupt()]
        with patch("telegram_bot.PrivateMessages") as private, patch("telegram_bot.InlineAnswers"), patch("telegram_bot.GuestAnswers") as guests:
            with self.assertRaises(KeyboardInterrupt): poll(api, DB(), "model")
            guests.return_value.submit.assert_called_once_with(guest)
            private.return_value.submit.assert_not_called()
            guests.return_value.close.assert_called_once()
            self.assertIn("guest_message", api.call.call_args_list[0].kwargs["allowed_updates"])


if __name__ == "__main__":
    unittest.main()
