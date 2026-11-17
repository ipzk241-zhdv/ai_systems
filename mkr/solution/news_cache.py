"""Periodic full scans and shared on-demand tail refreshes, without LLM calls."""
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import sys
from threading import Event, Thread, Lock
from time import monotonic

from news_history import initialize as initialize_history, save_articles, title_hash
from sources import Article, collect


DEFAULT_INTERVAL = 30 * 60
REFRESH_REUSE_SECONDS = 60
_refresh_locks = {}
_locks_guard = Lock()


def refresh_lock(db):
    path = db.execute("PRAGMA database_list").fetchone()[2]
    key = os.path.normcase(os.path.abspath(path)) if path else ("memory", id(db))
    with _locks_guard:
        return _refresh_locks.setdefault(key, Lock())


def poll_seconds(config):
    try:
        minutes = int(config.get("NEWS_POLL_MINUTES", "30"))
    except (ValueError, TypeError):
        raise ValueError("NEWS_POLL_MINUTES має бути цілим числом від 1 до 1440.") from None
    if not 1 <= minutes <= 1440:
        raise ValueError("NEWS_POLL_MINUTES має бути цілим числом від 1 до 1440.")
    return minutes * 60


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS news_fetch_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            until_at TEXT NOT NULL,
            status_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS news_articles_published ON news_articles(published);
    """)


def collect_once(db, until=None, collect_fn=None, since=None, previous_status=None):
    until = until or datetime.now(timezone.utc)
    # Full scans revisit 24h; a button refresh passes the last checkpoint as since.
    # Unique (URL, title hash) keeps repeated fetches from duplicating the archive.
    articles, status = (collect_fn or collect)(since or until - timedelta(hours=24), until)
    if not articles and status and all(error for _, error in status.values()):
        # A failed tail must not move the checkpoint past news we never obtained.
        raise ValueError("Не вдалося дозібрати новини.")
    if previous_status:
        status = {source: (status.get(source, (0, "Немає результату дозбору"))[0],
                           previous_status.get(source, (0, ""))[1] or
                           status.get(source, (0, "Немає результату дозбору"))[1])
                  for source in set(status) | set(previous_status)}
    save_articles(db, articles, until)
    with db:
        db.execute("""INSERT INTO news_fetch_runs (started_at, finished_at, until_at, status_json)
            VALUES (?, ?, ?, ?)""", (until.isoformat(), datetime.now(timezone.utc).isoformat(),
                                      until.isoformat(), json.dumps(status, ensure_ascii=False)))
        # Keep fetch diagnostics for a week; articles linked to digests are preserved.
        db.execute("DELETE FROM news_fetch_runs WHERE julianday(finished_at) < julianday(?)",
                   ((until - timedelta(days=7)).isoformat(),))
    return len(articles)


def latest_fetch(db):
    row = db.execute("SELECT until_at, status_json FROM news_fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None, {}
    return datetime.fromisoformat(row[0]), json.loads(row[1])


def refresh_news(db, until=None, full=False, collect_fn=None):
    target = until or datetime.now(timezone.utc)
    # Each caller owns its SQLite connection. The per-database lock covers network
    # collection too, so a button and a background scan cannot fetch in parallel.
    with refresh_lock(db):
        last, status = latest_fetch(db)
        age = (target - last).total_seconds() if last else None
        if last and (age <= 0 or (not full and age <= REFRESH_REUSE_SECONDS)):
            return last, status
        start = None if full or last is None else max(last, target - timedelta(hours=24))
        collect_once(db, target, collect_fn, since=start,
                     previous_status=status if start is not None else None)
        return latest_fetch(db)


def cached_articles(db, since, until, status):
    rows = db.execute("""SELECT a.title, a.url, a.published, a.source FROM news_articles a
        WHERE julianday(a.published) >= julianday(?) AND julianday(a.published) < julianday(?)
        AND a.id = (SELECT b.id FROM news_articles b WHERE b.url=a.url
                    ORDER BY julianday(b.last_seen) DESC, b.id DESC LIMIT 1)
        ORDER BY julianday(a.published) DESC, a.id DESC""", (since.isoformat(), until.isoformat())).fetchall()
    articles = [Article(title, url, datetime.fromisoformat(published), source)
                for title, url, published, source in rows]
    # Counts describe this digest's interval; fetch counts describe the 24h scan.
    sources = set(status) | {article.source for article in articles}
    coverage = {source: (sum(article.source == source for article in articles),
                         status.get(source, (0, "Немає результату останнього збору"))[1])
                for source in sorted(sources)}
    return articles, coverage


def cached_ids(db, articles):
    return [db.execute("SELECT id FROM news_articles WHERE url=? AND title_hash=?",
                       (article.url, title_hash(article.title))).fetchone()[0] for article in articles]


class NewsCollector:
    def __init__(self, database_path, interval=DEFAULT_INTERVAL, collect_fn=None):
        self.path, self.interval, self.collect_fn = database_path, interval, collect_fn
        self.failure = None
        self.stopped = Event()
        self.thread = Thread(target=self.run, name="news-collector", daemon=True)

    def start(self):
        self.thread.start()

    def check(self):
        if self.failure:
            raise self.failure

    def run(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=30)
            initialize_history(db)
            initialize(db)
            last, _ = latest_fetch(db)
            if last:
                delay = max(0, self.interval - (datetime.now(timezone.utc) - last).total_seconds())
                if self.stopped.wait(delay):
                    return
            while not self.stopped.is_set():
                started = monotonic()
                try:
                    refresh_news(db, full=True, collect_fn=self.collect_fn)
                    print("Background news scan completed.", flush=True)
                except Exception as error:
                    # Last good cache remains usable; the next cycle retries normally.
                    print("Background news scan failed (" + type(error).__name__ + ").", file=sys.stderr)
                if self.stopped.wait(max(0, self.interval - (monotonic() - started))):
                    break
        except Exception as error:
            self.failure = error
            print("Background news worker failed (" + type(error).__name__ + ").", file=sys.stderr)
        finally:
            if db is not None:
                db.close()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)
