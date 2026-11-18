import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import llm
import settings
import news_history


class Response(io.StringIO):
    def __init__(self, data, headers=None):
        super().__init__(json.dumps(data))
        self.headers = headers or {}


class ProviderTest(unittest.TestCase):
    def setUp(self):
        llm._remaining_tokens = None
        self.config = {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "test-secret"}
        self.model = "openai/gpt-oss-20b"

    def response(self, finish="stop"):
        return Response({"choices": [{"message": {"content": '{"status":"ok"}', "reasoning": "private"},
                                      "finish_reason": finish}], "usage": {"total_tokens": 12}})

    def call(self):
        return llm.chat([{"role": "user", "content": "Test"}], self.model,
                        options={"temperature": 0, "num_predict": 2400}, think=False,
                        schema={"type": "object"})

    def test_groq_request_and_response(self):
        with patch.object(llm, "read_config", return_value=self.config), patch.object(llm, "urlopen", return_value=self.response()) as request:
            result = self.call()
        req = request.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-secret")
        self.assertNotIn("test-secret", req.data.decode())
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["max_completion_tokens"], 2400)
        self.assertFalse(body["include_reasoning"])
        self.assertNotIn("think", body)
        self.assertNotIn("options", body)
        self.assertNotIn("reasoning", result["message"])
        self.assertEqual(result["usage"]["total_tokens"], 12)

    def test_ollama_compatibility(self):
        with patch.object(llm, "read_config", return_value={}), patch.object(llm, "urlopen", return_value=Response({"message": {"content": "ok"}})) as request:
            self.call()
        req = request.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertFalse(body["think"])
        self.assertEqual(body["format"], {"type": "object"})
        self.assertIsNone(req.get_header("Authorization"))

    def test_safe_auth_and_missing_key_errors(self):
        error = HTTPError("https://test", 401, "test-secret", {}, io.BytesIO(b"secret provider body"))
        with patch.object(llm, "read_config", return_value=self.config), patch.object(llm, "urlopen", side_effect=error):
            with self.assertRaises(llm.LLMError) as caught:
                self.call()
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertNotIn("secret provider body", str(caught.exception))
        with patch.object(llm, "read_config", return_value={"LLM_PROVIDER": "groq"}), patch.object(llm, "urlopen") as request:
            with self.assertRaises(llm.LLMError): self.call()
            request.assert_not_called()

    def test_schema_generation_error_is_typed_without_exposing_failed_generation(self):
        body={'request_id':'req_test','error':{'code':'json_validate_failed','message':'private provider message','failed_generation':'secret generated text'}}
        error=HTTPError('https://test',400,'bad json',{},io.BytesIO(json.dumps(body).encode()))
        with patch.object(llm,'read_config',return_value=self.config),patch.object(llm,'urlopen',side_effect=error) as request:
            with self.assertRaises(llm.LLMSchemaError) as caught:self.call()
        self.assertEqual(request.call_count,1)
        self.assertNotIn('secret generated',str(caught.exception))
        self.assertNotIn('private provider',str(caught.exception))

    def test_model_configuration_error_is_not_a_schema_retry(self):
        error=HTTPError('https://test',400,'bad model',{},io.BytesIO(b'{"error":{"code":"model_not_found"}}'))
        with patch.object(llm,'read_config',return_value=self.config),patch.object(llm,'urlopen',side_effect=error):
            with self.assertRaises(llm.LLMError) as caught:self.call()
        self.assertNotIsInstance(caught.exception,llm.LLMSchemaError)

    def test_short_rate_limit_retries_but_long_limit_does_not(self):
        short = HTTPError("https://test", 429, "limited", {"Retry-After": "2"}, io.BytesIO())
        with patch.object(llm, "read_config", return_value=self.config), patch.object(llm, "urlopen", side_effect=[short, self.response()]) as request, patch.object(llm, "sleep") as wait:
            self.call()
            self.assertEqual(request.call_count, 2)
            wait.assert_called_once_with(2)
        long = HTTPError("https://test", 429, "limited", {"Retry-After": "3600"}, io.BytesIO())
        with patch.object(llm, "read_config", return_value=self.config), patch.object(llm, "urlopen", side_effect=long) as request, patch.object(llm, "sleep") as wait:
            with self.assertRaises(llm.LLMError): self.call()
            self.assertEqual(request.call_count, 1)
            wait.assert_not_called()

    def test_truncated_response_is_preserved_for_callers(self):
        with patch.object(llm, "read_config", return_value=self.config), patch.object(llm, "urlopen", return_value=self.response("length")):
            self.assertEqual(self.call()["done_reason"], "length")

    def test_quota_headers_and_long_pause(self):
        with patch.object(llm, "monotonic", return_value=10):
            llm.remember_quota({"x-ratelimit-remaining-tokens": "1", "x-ratelimit-reset-tokens": "1m30s"})
            with self.assertRaises(llm.LLMError):
                llm.wait_for_quota({"messages": [], "max_completion_tokens": 2048})
        llm._remaining_tokens = None

    def test_config_and_model_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("LLM_PROVIDER=groq\nGROQ_API_KEY=file-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"GROQ_API_KEY": "environment-key"}, clear=True):
                config = settings.read_config(path)
            self.assertEqual(config["GROQ_API_KEY"], "environment-key")
            self.assertEqual(settings.default_model(config), self.model)
        self.assertEqual(settings.default_model({}), "qwen3.5:9b")
        with self.assertRaises(ValueError): settings.provider({"LLM_PROVIDER": "invalid"})

    def test_groq_history_chunks_keep_all_candidates(self):
        # A repeat found in a later history chunk must override an earlier update.
        from datetime import datetime, timezone
        from sources import Article
        article = Article("Новина", "https://example.org/1", datetime.now(timezone.utc), "test")
        item = {"id": 1, "section": "news"}
        history = [{"id": i, "title": str(i), "published": "2026-11-17T00:00:00+00:00"} for i in range(30)]
        replies = [{"comparisons": [{"status": "update", "previous": 1}]},
                   {"comparisons": [{"status": "repeat", "previous": 1}]}]
        with patch.object(news_history, "read_config", return_value=self.config), patch.object(news_history, "ollama_chat", side_effect=[{"message": {"content": json.dumps(reply)}} for reply in replies]) as request:
            selected, updates, repeated = news_history.compare_batch([article], [item], history, self.model)
            self.assertEqual(request.call_count, 2)
            self.assertEqual((selected, updates, repeated), ([], {}, 1))


if __name__ == "__main__":
    unittest.main()
