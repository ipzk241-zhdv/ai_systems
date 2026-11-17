"""One private-message worker, owning its own SQLite connection."""
from collections import OrderedDict
from queue import Queue, Empty, Full
import sys
from threading import Event, Thread


class PrivateMessages:
    def __init__(self, api, database_path, model, handler, open_database):
        self.api, self.path, self.model = api, database_path, model
        self.handler, self.open_database = handler, open_database
        self.queue = Queue(maxsize=16)
        self.stopped, self.ready = Event(), Event()
        self.failure = None
        self.thread = Thread(target=self.run, name="private-messages", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            raise ValueError("Private-message worker did not start")
        self.check()

    def check(self):
        if self.failure:
            raise self.failure

    def submit(self, update):
        self.check()
        try:
            self.queue.put_nowait(update)
        except Full:
            message = update.get("message", {})
            chat = message.get("chat", {})
            if chat.get("type") == "private":
                self.api.send(chat["id"], "Бот зайнятий. Повтори запит трохи пізніше.")

    def run(self):
        db = None
        try:
            db = self.open_database(self.path)
            self.ready.set()
            conversations = OrderedDict()
            while not self.stopped.is_set():
                try:
                    update = self.queue.get(timeout=.2)
                except Empty:
                    continue
                try:
                    self.handler(self.api, update, db, conversations, self.model)
                except Exception as error:
                    if getattr(error, "code", None) in {401, 404, 409}:
                        raise
                    print("Private request failed (" + type(error).__name__ + ").", file=sys.stderr)
                finally:
                    self.queue.task_done()
        except Exception as error:
            self.failure = error
            self.ready.set()
        finally:
            if db is not None:
                db.close()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)
