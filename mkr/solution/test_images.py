import base64
from collections import OrderedDict
import io
import json
import unittest
from unittest.mock import Mock, patch

import guest_bot
import image_notes as images
import llm
import telegram_bot as bot


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image-data"


def photo_message(**extra):
    return {"chat": {"type": "private", "id": 123}, "message_id": 7,
            "photo": [{"file_id": "small", "width": 20, "height": 20},
                      {"file_id": "large", "width": 100, "height": 100}], **extra}


class Response(io.BytesIO):
    headers = {}


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.config = {"GROQ_API_KEY": "groq-test"}
        self.api = Mock()
        self.api.download_image.return_value = PNG
        self.answer = {"message": {"content": "**Текст** <unsafe>"}, "done_reason": "stop"}

    def test_request_contains_image_bytes_and_own_model_without_history_or_telegram_url(self):
        with patch.object(images, "read_config", return_value=self.config), patch.object(images, "groq_chat", return_value=self.answer) as generate:
            images.analyze_message(self.api, photo_message(caption="Що тут написано?"))
        self.api.download_image.assert_called_once_with("large", images.MAX_BYTES)
        messages, model = generate.call_args.args[:2]
        self.assertEqual(model, images.DEFAULT_MODEL)
        self.assertEqual(len(messages), 2)
        content = messages[1]["content"]
        self.assertEqual(content[0]["text"], "Що тут написано?")
        uri = content[1]["image_url"]["url"]
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(uri.partition(",")[2]), PNG)
        self.assertNotIn("telegram.org", json.dumps(messages))
        self.assertTrue(generate.call_args.kwargs["fast"])

    def test_choose_largest_fitting_photo_and_detect_formats(self):
        value = photo_message()
        value["photo"][1]["file_size"] = images.MAX_BYTES + 1
        self.assertEqual(images.image_file(value)["file_id"], "small")
        for data, mime in ((PNG, "image/png"), (b"\xff\xd8\xffabc", "image/jpeg"), (b"RIFF1234WEBP123", "image/webp")):
            self.assertEqual(images.image_mime(data), mime)
        with self.assertRaises(llm.LLMError):
            images.image_mime(b"<svg>not a supported raster image</svg>")

    def test_direct_and_quoted_sources_and_image_document(self):
        value = photo_message()
        self.assertIs(images.image_source(value), value)
        self.assertIs(images.image_source({"reply_to_message": value}), value)
        document = {"document": {"mime_type": "image/png", "file_id": "file"}}
        self.assertIs(images.image_source(document), document)
        self.assertIsNone(images.image_source({"document": {"mime_type": "application/pdf"}}))

    def test_size_key_prompt_and_bytes_validated_before_model(self):
        cases = [({}, photo_message(), PNG),
                 (self.config, {"document": {"mime_type": "image/png", "file_id": "file", "file_size": images.MAX_BYTES + 1}}, PNG),
                 (self.config, photo_message(caption="x" * 1501), PNG),
                 (self.config, photo_message(), b"not an image"),
                 (self.config, photo_message(), b""),
                 (self.config, photo_message(), b"x" * (images.MAX_BYTES + 1))]
        for config, message, data in cases:
            self.api.download_image.return_value = data
            with patch.object(images, "read_config", return_value=config), patch.object(images, "groq_chat") as generate:
                with self.assertRaises(llm.LLMError):
                    images.analyze_message(self.api, message)
                generate.assert_not_called()

    def test_custom_model_default_prompt_and_truncation_notice(self):
        response = {"message": {"content": "text"}, "done_reason": "length"}
        with patch.object(images, "read_config", return_value={**self.config, "VISION_MODEL": "custom"}), patch.object(images, "groq_chat", return_value=response) as generate:
            answer = images.analyze_message(self.api, photo_message())
        self.assertIn("ліміту", answer)
        self.assertEqual(generate.call_args.args[1], "custom")
        self.assertEqual(generate.call_args.args[0][1]["content"][0]["text"], images.DEFAULT_QUESTION)

    def test_private_routing_single_safe_reply_and_text_only_history(self):
        history = OrderedDict()
        with patch.object(images, "analyze_message", return_value=self.answer["message"]["content"]):
            bot.handle_update(self.api, {"update_id": 1, "message": photo_message()}, None, history, "text-model")
        self.api.send.assert_called_once_with(123, "<b>Текст</b> &lt;unsafe&gt;", parse_mode="HTML", link_preview_options={"is_disabled": True})
        self.assertNotIn("file_id", json.dumps(history))
        self.assertIn("сама картинка не зберігається", history[123][1]["content"])

    def test_private_long_reply_is_one_complete_file(self):
        answer = "<long>" * 1000
        with patch.object(images, "analyze_message", return_value=answer):
            images.handle_image(self.api, 123, photo_message(), OrderedDict(), bot.format_chat_html)
        self.api.send.assert_not_called()
        self.api.send_document.assert_called_once()
        self.assertIn("&lt;long&gt;" * 1000, self.api.send_document.call_args.args[2])

    def test_guest_reply_only_mention_on_photo_and_group_caption(self):
        generate = Mock()
        worker = guest_bot.GuestAnswers(self.api, "text-model", bot.format_chat_html, generate=generate)
        message = {"text": "@DBresh_bot", "guest_query_id": "query", "reply_to_message": photo_message()}
        with patch.object(guest_bot, "analyze_message", return_value="**Result** <unsafe>") as analyze:
            worker.process(message)
        analyze.assert_called_once_with(self.api, message, "")
        generate.assert_not_called()
        self.api.send.assert_not_called()
        self.assertEqual(self.api.call.call_args.args[0], "answerGuestQuery")
        self.assertEqual(self.api.call.call_args.kwargs["result"]["input_message_content"]["message_text"], "<b>Result</b> &lt;unsafe&gt;")
        group = photo_message(chat={"id": 123, "type": "group"}, caption="@DBresh_bot поясни")
        self.assertTrue(worker.accepts(group))
        with patch.object(guest_bot, "analyze_message", return_value="result") as analyze:
            worker.process(group)
        analyze.assert_called_once_with(self.api, group, "поясни")
        self.assertEqual(self.api.send.call_args.kwargs["reply_parameters"]["message_id"], 7)

    def test_long_guest_answer_marks_truncation_and_remains_one_message(self):
        worker = guest_bot.GuestAnswers(self.api, "text-model", bot.format_chat_html)
        with patch.object(guest_bot, "analyze_message", return_value="😀" * 5000):
            worker.process({"text": "@DBresh_bot", "guest_query_id": "query", "reply_to_message": photo_message()})
        self.api.call.assert_called_once()
        content = self.api.call.call_args.kwargs["result"]["input_message_content"]["message_text"]
        self.assertIn("Відповідь скорочена", content)
        self.assertLess(len(content.encode("utf-16-le")), 8192)

    def test_download_failure_is_safe_and_does_not_kill_private_worker(self):
        with patch.object(images, "analyze_message", side_effect=bot.TelegramError(400)):
            images.handle_image(self.api, 123, photo_message(), OrderedDict(), bot.format_chat_html)
        self.api.send.assert_called_once()
        self.assertIn("особистий чат", self.api.send.call_args.args[1])

    def test_secure_download_path_and_bounded_bytes(self):
        api = bot.Telegram("fake-secret")
        with patch.object(api, "call", return_value={"file_path": "photos/file.jpg", "file_size": len(PNG)}), patch.object(bot, "urlopen", return_value=Response(PNG)):
            self.assertEqual(api.download_image("file", 100), PNG)
        for path in ("../file", "/file", "photos/../file", "https://bad", "photos\\file"):
            with patch.object(api, "call", return_value={"file_path": path}), patch.object(bot, "urlopen") as download:
                with self.assertRaises(llm.LLMError):
                    api.download_image("file", 100)
                download.assert_not_called()
        with patch.object(api, "call", return_value={"file_path": "photos/file.jpg"}), patch.object(bot, "urlopen", return_value=Response(b"x" * 101)):
            with self.assertRaises(llm.LLMError):
                api.download_image("file", 100)

    def test_qwen_transport_does_not_estimate_base64_as_text_tokens(self):
        llm._remaining_tokens = 6000
        llm._quota_model = images.DEFAULT_MODEL
        llm._reset_at = llm.monotonic() + 10
        result = {"choices": [{"message": {"content": "result"}, "finish_reason": "stop"}]}
        response = Response(json.dumps(result).encode())
        try:
            with patch.object(llm, "urlopen", return_value=response) as send, patch.object(llm, "sleep") as wait:
                messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 100000}}]}]
                llm.groq_chat(messages, images.DEFAULT_MODEL, {}, None, self.config, 30, fast=True)
            wait.assert_not_called()
            self.assertEqual(json.loads(send.call_args.args[0].data)["reasoning_effort"], "none")
        finally:
            llm._remaining_tokens = None
            llm._quota_model = None


if __name__ == "__main__":
    unittest.main()
