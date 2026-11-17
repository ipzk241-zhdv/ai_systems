from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread, get_ident
import unittest
from unittest.mock import Mock, patch

import news_cache as cache
from news_history import initialize, save_articles
from sources import Article
import telegram_bot as bot


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.open_state(":memory:")
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.article = Article("Новина", "https://example.org/news", self.now - timedelta(minutes=15), "source")
        self.fetch = Mock(return_value=([self.article], {"source": (1, "")}))

    def tearDown(self):
        self.db.close()

    def test_background_stores_articles_and_status_without_calling_ai(self):
        with patch("news_history.ollama_chat") as ai:
            cache.collect_once(self.db, self.now, self.fetch)
            ai.assert_not_called()
        self.fetch.assert_called_once_with(self.now - timedelta(hours=24), self.now)
        checked, status = cache.latest_fetch(self.db)
        self.assertEqual(checked, self.now)
        self.assertEqual(status, {"source": [1, ""]})
        articles, coverage = cache.cached_articles(self.db, self.now - timedelta(hours=4), self.now, status)
        self.assertEqual(articles, [self.article])
        self.assertEqual(coverage, {"source": (1, "")})
        self.assertEqual(len(cache.cached_ids(self.db, articles)), 1)

    def test_repeated_scans_do_not_duplicate_and_latest_title_wins(self):
        cache.collect_once(self.db, self.now, self.fetch)
        cache.collect_once(self.db, self.now + timedelta(minutes=30), self.fetch)
        self.assertEqual(self.db.execute("SELECT count(*) FROM news_articles").fetchone()[0], 1)
        changed = Article("Оновлена новина", self.article.url, self.article.published, "source")
        cache.collect_once(self.db, self.now + timedelta(hours=1), lambda *args: ([changed], {"source": (1, "")}))
        # If a publisher restores an earlier title, last_seen wins over insertion ID.
        cache.collect_once(self.db, self.now + timedelta(hours=2), self.fetch)
        articles, _ = cache.cached_articles(self.db, self.now - timedelta(hours=4), self.now, {})
        self.assertEqual(articles, [self.article])
        self.assertEqual(self.db.execute("SELECT count(*) FROM news_articles").fetchone()[0], 2)

    def test_period_filters_offsets_and_counts_and_keeps_failure_warning(self):
        offset = timezone(timedelta(hours=2))
        old = Article("Old", "https://example.org/old", self.now - timedelta(hours=5), "source")
        local = Article("Local", "https://example.org/local", self.article.published.astimezone(offset), "source")
        save_articles(self.db, [old, local], self.now)
        articles, coverage = cache.cached_articles(self.db, self.now - timedelta(hours=4), self.now, {"source": [50, "partial"]})
        self.assertEqual(articles, [local])
        self.assertEqual(coverage, {"source": (1, "partial")})

    def test_failed_collection_preserves_last_good_checkpoint_and_articles(self):
        cache.collect_once(self.db, self.now, self.fetch)
        with self.assertRaises(OSError):
            cache.collect_once(self.db, self.now + timedelta(minutes=30), Mock(side_effect=OSError("offline")))
        self.assertEqual(cache.latest_fetch(self.db)[0], self.now)
        self.assertEqual(self.db.execute("SELECT count(*) FROM news_articles").fetchone()[0], 1)

    def test_digest_uses_cache_and_does_not_fetch_sites(self):
        cache.collect_once(self.db, self.now, self.fetch)
        api = Mock()
        update = {"update_id": 1, "message": {"chat": {"id": 123, "type": "private"}, "text": "За 4 години"}}
        with patch.object(cache, "collect", side_effect=AssertionError("must not fetch on demand")), patch.object(bot, "select_items", return_value=[{"id": 1, "section": "news"}]) as select, patch.object(bot, "compare_items", return_value=([{"id": 1, "section": "news"}], {}, 0)), patch.object(bot, "fetch_market", return_value={}), patch.object(bot, "render_html", return_value="<html>cached</html>"):
            bot.handle_update(api, update, self.db, OrderedDict(), "model")
        self.assertEqual(select.call_args.args[0], [self.article])
        api.send_document.assert_called_once()
        self.assertEqual(self.db.execute("SELECT checked_at FROM checks").fetchone()[0], self.now.isoformat())
        self.assertIsNotNone(self.db.execute("SELECT delivered_at FROM news_digests").fetchone()[0])

    def test_new_install_collects_initial_day_before_digest(self):
        api = Mock()
        with patch.object(cache, "collect", return_value=([], {"source": (0, "")})) as fetch, patch.object(bot, "select_items", return_value=[]) as select, patch.object(bot, "compare_items", return_value=([], {}, 0)), patch.object(bot, "fetch_market", return_value={}), patch.object(bot, "render_html", return_value="html"):
            bot.handle_update(api, {"update_id": 1, "message": {"chat": {"id": 123, "type": "private"}, "text": "За 24 години"}}, self.db, OrderedDict(), "model")
        fetch.assert_called_once()
        since, until = fetch.call_args.args
        self.assertEqual(until - since, timedelta(hours=24))
        select.assert_called_once()
        api.send_document.assert_called_once()

    def test_old_cache_is_explicitly_marked(self):
        old_time = self.now - timedelta(hours=2)
        old_article = Article("Old cache", self.article.url, old_time - timedelta(minutes=15), "source")
        cache.collect_once(self.db, old_time, lambda *args: ([old_article], {"source": (1, "")}))
        api = Mock()
        with patch.object(bot, "refresh_news", side_effect=OSError("offline")), patch.object(bot, "select_items", return_value=[]), patch.object(bot, "compare_items", return_value=([], {}, 0)), patch.object(bot, "fetch_market", return_value={}), patch.object(bot, "render_html", return_value="html"):
            bot.handle_update(api, {"update_id": 1, "message": {"chat": {"id": 123, "type": "private"}, "text": "За 4 години"}}, self.db, OrderedDict(), "model")
        self.assertIn("застаріли", api.send.call_args_list[1].args[1])
        self.assertIn("Дозбір не вдався", api.send.call_args_list[1].args[1])

    def test_on_demand_fetches_only_missing_tail_and_combines_with_history(self):
        before = self.now - timedelta(minutes=30)
        old = Article("Раніше", "https://example.org/previous", before - timedelta(hours=2), "source")
        cache.collect_once(self.db, before, lambda *args: ([old], {"source": (1, "")}))
        fetch = Mock(return_value=([self.article], {"source": (1, "")}))
        checked, status = cache.refresh_news(self.db, until=self.now, collect_fn=fetch)
        fetch.assert_called_once_with(before, self.now)
        articles, _ = cache.cached_articles(self.db, self.now - timedelta(hours=24), self.now, status)
        self.assertEqual(articles, [self.article, old])
        self.assertEqual(checked, self.now)

    def test_digest_selection_includes_freshly_collected_tail(self):
        before = self.now - timedelta(minutes=30)
        cache.collect_once(self.db, before, lambda *args: ([], {"source": (0, "")}))
        api = Mock()
        with patch.object(cache, "collect", return_value=([self.article], {"source": (1, "")})) as fetch, patch.object(bot, "select_items", return_value=[]) as select, patch.object(bot, "compare_items", return_value=([], {}, 0)), patch.object(bot, "fetch_market", return_value={}), patch.object(bot, "render_html", return_value="html"):
            bot.handle_update(api, {"update_id": 1, "message": {"chat": {"id": 123, "type": "private"}, "text": "За 24 години"}}, self.db, OrderedDict(), "model")
        self.assertEqual(fetch.call_args.args[0], before)
        self.assertEqual(select.call_args.args[0], [self.article])
        api.send_document.assert_called_once()

    def test_recent_refresh_is_reused_and_long_gaps_are_capped_at_day(self):
        cache.collect_once(self.db, self.now, self.fetch)
        fetch = Mock(return_value=([], {"source": (0, "")}))
        cache.refresh_news(self.db, self.now + timedelta(seconds=30), collect_fn=fetch)
        fetch.assert_not_called()
        later = self.now + timedelta(days=3)
        cache.refresh_news(self.db, later, collect_fn=fetch)
        fetch.assert_called_once_with(later - timedelta(hours=24), later)

    def test_failed_tail_does_not_advance_checkpoint_or_erase_old_cache(self):
        cache.collect_once(self.db, self.now, self.fetch)
        fetch = Mock(return_value=([], {"source": (0, "offline")}))
        with self.assertRaises(ValueError):
            cache.refresh_news(self.db, self.now + timedelta(minutes=30), collect_fn=fetch)
        self.assertEqual(cache.latest_fetch(self.db)[0], self.now)
        self.assertEqual(self.db.execute("SELECT count(*) FROM news_articles").fetchone()[0], 1)

    def test_incremental_success_does_not_hide_earlier_coverage_gap(self):
        cache.collect_once(self.db, self.now, lambda *args: ([self.article], {"source": (1, "incomplete day")}))
        _, status = cache.refresh_news(self.db, self.now + timedelta(minutes=30), collect_fn=lambda *args: ([], {"source": (0, "")}))
        self.assertEqual(status["source"][1], "incomplete day")
        _, status = cache.refresh_news(self.db, self.now + timedelta(minutes=60), full=True, collect_fn=self.fetch)
        self.assertEqual(status["source"][1], "")

    def test_simultaneous_background_and_button_share_collection(self):
        entered, release, waiting = Event(), Event(), Event()
        failures = []
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "shared.sqlite3")
            db = bot.open_state(path)
            cache.collect_once(db, self.now - timedelta(minutes=30), self.fetch)
            db.close()
            def fetch(*args):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test timeout")
                return [self.article], {"source": (1, "")}
            fetch_mock = Mock(side_effect=fetch)
            def run(full):
                db = sqlite3.connect(path, timeout=5)
                try:
                    if not full:
                        waiting.set()
                    cache.refresh_news(db, self.now, full=full, collect_fn=fetch_mock)
                except Exception as error:
                    failures.append(error)
                finally:
                    db.close()
            first, second = Thread(target=run, args=(True,)), Thread(target=run, args=(False,))
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                second.start()
                self.assertTrue(waiting.wait(2))
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, [])
            fetch_mock.assert_called_once_with(self.now - timedelta(hours=24), self.now)

    def test_worker_owns_connection_and_retries_after_failed_cycle(self):
        done = Event()
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            def fetch(*args):
                calls.append(get_ident())
                if len(calls) == 1:
                    raise ValueError("temporary failure")
                worker.stopped.set()
                done.set()
                return [self.article], {"source": (1, "")}
            worker = cache.NewsCollector(str(path), interval=.01, collect_fn=fetch)
            worker.start()
            try:
                self.assertTrue(done.wait(3))
            finally:
                worker.close()
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0], get_ident())
            db = sqlite3.connect(str(path))
            try:
                self.assertEqual(db.execute("SELECT count(*) FROM news_articles").fetchone()[0], 1)
            finally:
                db.close()

    def test_restart_reuses_recent_scan_and_close_interrupts_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            db = bot.open_state(path)
            cache.collect_once(db, self.now, self.fetch)
            db.close()
            loaded = Event()
            real_latest = cache.latest_fetch
            def latest(db):
                result = real_latest(db)
                loaded.set()
                return result
            fetch = Mock()
            worker = cache.NewsCollector(str(path), collect_fn=fetch)
            with patch.object(cache, "latest_fetch", side_effect=latest):
                worker.start()
                try:
                    self.assertTrue(loaded.wait(3))
                finally:
                    worker.close()
            self.assertFalse(worker.thread.is_alive())
            fetch.assert_not_called()

    def test_poll_starts_and_stops_collector_with_other_workers(self):
        class DB:
            def execute(self, *args): return self
            def fetchone(self): return (0, "main", "example.sqlite3")
        api = Mock()
        api.call.side_effect = KeyboardInterrupt()
        with patch.object(bot, "NewsCollector") as collector, patch.object(bot, "PrivateMessages"), patch.object(bot, "InlineAnswers"), patch.object(bot, "GuestAnswers"):
            with self.assertRaises(KeyboardInterrupt):
                bot.poll(api, DB(), "model")
        collector.return_value.start.assert_called_once()
        collector.return_value.close.assert_called_once()

    def test_interval_setting_validation(self):
        self.assertEqual(cache.poll_seconds({}), 1800)
        self.assertEqual(cache.poll_seconds({"NEWS_POLL_MINUTES": "15"}), 900)
        for value in ("0", "1441", "no", "1.5", ""):
            with self.assertRaises(ValueError):
                cache.poll_seconds({"NEWS_POLL_MINUTES": value})

    def test_worker_initialization_failure_is_reported_to_service_monitor(self):
        worker = cache.NewsCollector("unused.sqlite3")
        with patch.object(cache.sqlite3, "connect", side_effect=OSError("unavailable")):
            worker.start()
            worker.thread.join(timeout=2)
        self.assertFalse(worker.thread.is_alive())
        with self.assertRaises(OSError):
            worker.check()


if __name__ == "__main__":
    unittest.main()
