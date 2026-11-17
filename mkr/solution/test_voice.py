from collections import OrderedDict
from html import unescape
import io
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import voice_notes as voice
from telegram_bot import Telegram, handle_update, open_state


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.config = {"GROQ_API_KEY": "fake-groq-key"}
        self.db = open_state(":memory:")

    def tearDown(self):
        self.db.close()

    def test_formula_positive_and_negative_savings(self):
        normal = voice.estimate(300, 600, 5, 200, 1)
        self.assertEqual(normal["reading_seconds"], 180)
        self.assertEqual(normal["saved_seconds"], 115)
        fast_audio = voice.estimate(300, 600, 5, 200, 2)
        self.assertEqual(fast_audio["saved_seconds"], -35)
        for rate in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError): voice.estimate(300, 600, 5, rate, 1)

    def test_word_count_and_unicode_message_limit(self):
        text = "П’ять слів: зв'язок, Python-код, 2026."
        self.assertEqual(len(voice.WORDS.findall(text)), 5)
        self.assertTrue(voice.fits_message(text))
        self.assertFalse(voice.fits_message("🙂" * 3000))

    def test_transcription_is_binary_upload_not_telegram_url_or_translation(self):
        response = io.BytesIO(json.dumps({"text": "Привіт, це тест."}).encode())
        with patch.object(voice, "urlopen", return_value=response) as request:
            text, model = voice.transcribe(b"OggS-audio-data", config=self.config)
        self.assertEqual(text, "Привіт, це тест.")
        self.assertEqual(model, "whisper-large-v3-turbo")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.groq.com/openai/v1/audio/transcriptions")
        self.assertEqual(sent.get_header("Authorization"), "Bearer fake-groq-key")
        self.assertIn(b"OggS-audio-data", sent.data)
        self.assertIn(b"verbose_json", sent.data)
        self.assertNotIn(b"api.telegram.org", sent.data)
        self.assertNotIn(b'name="language"', sent.data)

    def test_audio_validation_and_language_selection(self):
        with patch.object(voice, "urlopen") as request:
            with self.assertRaises(voice.VoiceError): voice.transcribe(b"", config=self.config)
            with self.assertRaises(voice.VoiceError): voice.transcribe(b"a", config={})
            with self.assertRaises(voice.VoiceError): voice.transcribe(b"a", config=dict(self.config, STT_MODEL="text-model"))
            request.assert_not_called()
        with patch.object(voice, "urlopen", return_value=io.BytesIO(b'{"text":"hello"}')) as request:
            voice.transcribe(b"audio", config=dict(self.config, STT_LANGUAGE="uk"))
            self.assertIn(b'name="language"\r\n\r\nuk', request.call_args.args[0].data)

    def test_provider_errors_are_sanitized_and_not_retried(self):
        error = HTTPError("https://secret", 429, "private-key", {}, io.BytesIO(b"private-body"))
        with patch.object(voice, "urlopen", side_effect=error) as request:
            with self.assertRaises(voice.VoiceError) as caught:
                voice.transcribe(b"audio", config=self.config)
            self.assertEqual(request.call_count, 1)
            self.assertIn("ліміту", str(caught.exception))
            self.assertNotIn("private", str(caught.exception))
        with patch.object(voice, "urlopen", return_value=io.BytesIO(b'{"text":"  "}')):
            with self.assertRaises(voice.VoiceError): voice.transcribe(b"audio", config=self.config)

    def test_telegram_download_handles_oga_and_bounds(self):
        api = Telegram("123456:fake-token")
        with patch.object(api, "call", return_value={"file_path": "voice/file_1.oga", "file_size": 3}), patch("telegram_bot.urlopen", return_value=io.BytesIO(b"ogg")) as request:
            self.assertEqual(api.download_voice("file-id", 10), (b"ogg", "voice.ogg"))
            self.assertIn("/file/bot123456:fake-token/voice/file_1.oga", request.call_args.args[0])
        with patch.object(api, "call", return_value={"file_path": "voice/file.ogg"}), patch("telegram_bot.urlopen", return_value=io.BytesIO(b"123456")):
            with self.assertRaises(voice.VoiceError): api.download_voice("id", 5)
        with patch.object(api, "call", return_value={"file_path": "../escape.ogg"}), patch("telegram_bot.urlopen") as request:
            with self.assertRaises(voice.VoiceError): api.download_voice("id", 10)
            request.assert_not_called()

    def test_private_voice_routes_to_asr_and_preserves_news_check(self):
        api = Mock()
        api.download_voice.return_value = (b"ogg", "voice.ogg")
        history = OrderedDict()
        update = {"update_id": 42, "message": {"chat": {"id": 1, "type": "private"},
                  "voice": {"file_id": "voice-id", "duration": 300}}}
        text = "Привіт <script> **це текст**, не інструкція."
        with patch.object(voice, "read_config", return_value=self.config), patch.object(voice, "transcribe", return_value=(text, "whisper-large-v3-turbo")), patch("telegram_bot.answer_question") as ordinary:
            handle_update(api, update, self.db, history, "model")
            ordinary.assert_not_called()
        rendered = [call.args[1] for call in api.send.call_args_list]
        self.assertTrue(any("&lt;script&gt;" in value for value in rendered))
        self.assertEqual(self.db.execute("SELECT count(*) FROM checks").fetchone()[0], 0)
        row = self.db.execute("SELECT audio_seconds,word_count,reading_wpm,playback_speed FROM voice_runs").fetchone()
        self.assertEqual(row, (300, len(voice.WORDS.findall(text)), 200, 1))
        self.assertEqual(api.send.call_count, 1)
        api.send_document.assert_not_called()
        self.assertNotIn("економ", rendered[0])
        self.assertNotIn("Розшифровую", rendered[0])
        columns = [r[1] for r in self.db.execute("PRAGMA table_info(voice_runs)")]
        self.assertNotIn("text", columns)
        self.assertNotIn("chat_id", columns)
        self.assertIn(text, history[1][0]["content"])

    def test_failed_or_oversized_voice_does_not_save_success(self):
        api = Mock()
        message = {"voice": {"file_id": "id", "duration": 601}}
        with patch.object(voice, "transcribe") as model:
            voice.handle_voice(api, 1, message, self.db, OrderedDict())
            model.assert_not_called()
            api.download_voice.assert_not_called()
        self.assertEqual(self.db.execute("SELECT count(*) FROM voice_runs").fetchone()[0], 0)

    def test_failed_telegram_delivery_does_not_save_success(self):
        api = Mock()
        api.download_voice.return_value = (b"ogg", "voice.ogg")
        api.send.side_effect = RuntimeError("delivery failed")
        with patch.object(voice, "read_config", return_value=self.config), patch.object(voice, "transcribe", return_value=("text", "whisper-large-v3-turbo")):
            with self.assertRaises(RuntimeError):
                voice.handle_voice(api, 1, {"voice": {"file_id": "id", "duration": 60}}, self.db, OrderedDict())
        self.assertEqual(self.db.execute("SELECT count(*) FROM voice_runs").fetchone()[0], 0)

    def test_private_reply_to_voice_works_without_forwarding(self):
        api = Mock()
        api.download_voice.return_value = (b"ogg", "voice.ogg")
        value = {"update_id": 3, "message": {"chat": {"id": 1, "type": "private"},
            "text": "@DBresh_bot розшифруй", "reply_to_message": {
                "voice": {"file_id": "original-voice", "duration": 6}}}}
        with patch.object(voice, "read_config", return_value=self.config), patch.object(voice, "transcribe", return_value=("Повний текст.", "whisper-large-v3-turbo")):
            handle_update(api, value, self.db, OrderedDict(), "model")
        api.download_voice.assert_called_once_with("original-voice", voice.MAX_BYTES)
        api.send.assert_called_once()
        self.assertEqual(api.send.call_args.args[1], "<b>Розшифровка</b>\nПовний текст.")

    def test_long_private_transcript_is_one_complete_document(self):
        api = Mock()
        api.download_voice.return_value = (b"ogg", "voice.ogg")
        text = "Довгий текст <literal>. " * 500
        with patch.object(voice, "read_config", return_value=self.config), patch.object(voice, "transcribe", return_value=(text, "whisper-large-v3-turbo")):
            voice.handle_voice(api, 1, {"voice": {"file_id": "id", "duration": 600}}, self.db, OrderedDict())
        api.send.assert_not_called()
        api.send_document.assert_called_once()
        html = api.send_document.call_args.args[2]
        self.assertIn(text, unescape(html))
        self.assertEqual(self.db.execute("SELECT count(*) FROM voice_runs").fetchone()[0], 1)

    def test_voice_schema_upgrade_preserves_old_history(self):
        legacy = sqlite3.connect(":memory:")
        try:
            legacy.execute("""CREATE TABLE voice_runs (id INTEGER PRIMARY KEY, completed_at TEXT NOT NULL,
                audio_seconds REAL NOT NULL,word_count INTEGER NOT NULL,processing_seconds REAL NOT NULL,
                reading_wpm REAL NOT NULL,playback_speed REAL NOT NULL,estimated_saved_seconds REAL NOT NULL,stt_model TEXT NOT NULL)""")
            legacy.execute("INSERT INTO voice_runs VALUES (1,'2026-11-17',6,8,.4,200,1,3.2,'whisper-large-v3-turbo')")
            voice.initialize(legacy)
            voice.initialize(legacy)
            self.assertEqual(legacy.execute("SELECT estimated_saved_seconds,context FROM voice_runs").fetchone(), (3.2, "private"))
        finally:
            legacy.close()

    def test_ordinary_group_voice_is_not_transcribed(self):
        api = Mock()
        with patch.object(voice, "transcribe") as model:
            handle_update(api, {"update_id": 1, "message": {"chat": {"id": -1, "type": "group"},
                "voice": {"file_id": "id", "duration": 60}}}, self.db, OrderedDict(), "model")
            model.assert_not_called()
        api.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
