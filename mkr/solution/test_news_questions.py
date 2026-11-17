import unittest
import os
import tempfile
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bs4 import BeautifulSoup

import news_questions as questions
from html_digest import render_html
from news_history import save_articles, prepare_digest, confirm_delivery
from sources import Article
from telegram_bot import open_state, handle_update


class QuestionsTest(unittest.TestCase):
    def setUp(self):
        self.db = open_state(":memory:")
        self.now = datetime.now(timezone.utc)
        self.articles = [Article("Звичайна новина", "https://suspilne.media/1/", self.now, "Суспільне Новини"),
                         Article("Важлива новина", "https://suspilne.media/2/", self.now, "Суспільне Новини")]

    def tearDown(self):
        self.db.close()

    def digest(self, chat=1, delivered=True):
        ids = save_articles(self.db, self.articles, self.now)
        items = [{"id": 1, "section": "news"}, {"id": 2, "section": "important"}]
        html = render_html(self.articles, items, {}, {}, self.now - timedelta(hours=4), self.now)
        digest_id = prepare_digest(self.db, chat, self.now, self.now, html, self.articles, ids, items, {})
        if delivered:
            confirm_delivery(self.db, digest_id, chat, self.now, digest_id)
        return html

    def test_order_and_isolation(self):
        html = self.digest()
        self.digest(chat=2)
        self.digest(delivered=False)
        history = questions.delivered(self.db, 1)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["items"][0]["title"], "Важлива новина")
        self.assertIn("1. Важлива новина", html)
        self.assertEqual(questions.delivered(self.db, 3), [])
        self.assertIn("лише один", questions.respond(self.db, 1, "Що змінилося", "test"))
        self.assertIn("Спочатку", questions.respond(self.db, 3, "Поясни 1", "test"))
        self.assertIn("новин: 2", questions.respond(self.db, 1, "Поясни 0", "test"))

    def test_grounded_explanation_and_cache(self):
        self.digest()
        soup = BeautifulSoup('<article><p>' + "Факти статті. " * 25 + '</p></article>', "html.parser")
        with patch.object(questions, "page", return_value=soup) as fetch, patch.object(questions, "generate", return_value="Пояснення") as model:
            answer = questions.respond(self.db, 1, "Поясни 1: що сталося?", "test")
            self.assertIn("https://suspilne.media/2/", answer)
            self.assertIn("Факти статті", model.call_args[0][2]["article"])
            questions.respond(self.db, 1, "Поясни 1", "test")
            self.assertEqual(fetch.call_count, 1)
        with patch.object(questions, "page", side_effect=ValueError("offline")), patch.object(questions, "generate") as model:
            self.assertIn("Не вдалося завантажити", questions.respond(self.db, 1, "Поясни 2", "test"))
            model.assert_not_called()

    def test_comparison_and_routing(self):
        self.digest()
        self.digest()
        with patch.object(questions, "generate", return_value="Зміни") as model:
            answer = questions.respond(self.db, 1, "Що змінилося?", "test")
            self.assertIn("За заголовками", answer)
            self.assertEqual(len(model.call_args[0][2]["previous_digest"]["items"]), 2)
        class API:
            def __init__(self): self.sent = []
            def call(self, *args, **kwargs): pass
            def send(self, chat_id, text, **kwargs): self.sent.append(text)
        api = API()
        before = self.db.execute("SELECT * FROM checks").fetchall()
        with patch("telegram_bot.news_response", return_value="**Пояснення**"), patch("telegram_bot.answer_question") as ordinary:
            handle_update(api, {"update_id": 10, "message": {"chat": {"id": 1, "type": "private"}, "text": "Поясни 1"}}, self.db, OrderedDict(), "test")
            ordinary.assert_not_called()
        self.assertEqual(api.sent, ["<b>Пояснення</b>"])
        self.assertEqual(before, self.db.execute("SELECT * FROM checks").fetchall())

    def test_article_body_preferred_to_short_metadata(self):
        soup = BeautifulSoup('<script type="application/ld+json">' +
                             '{"articleBody": "' + 'Вступ. ' * 30 + '"}</script>' +
                             '<div class="c-article-content"><p>' + 'Повний текст. ' * 40 +
                             '</p><aside>Реклама</aside></div>', "html.parser")
        text = questions.extract_text(soup)
        self.assertIn("Повний текст", text)
        self.assertNotIn("Реклама", text)
        with self.assertRaises(ValueError):
            questions.extract_text(BeautifulSoup('<nav><p>Посилання</p></nav>', "html.parser"))

    def test_history_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.sqlite3")
            self.db.close()
            self.db = open_state(path)
            self.digest()
            self.db.close()
            self.db = open_state(path)
            self.assertEqual(questions.delivered(self.db, 1)[0]["items"][0]["number"], 1)
            self.db.close()
            self.db = open_state(":memory:")


if __name__ == "__main__":
    unittest.main()
