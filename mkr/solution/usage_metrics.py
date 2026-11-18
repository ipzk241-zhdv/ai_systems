"""Observed operational metrics; no prompts, filenames, paths, secrets or file contents."""
from contextlib import contextmanager
from contextvars import ContextVar
import csv
from functools import wraps
import io
import math
from pathlib import Path
import sqlite3
import sys
from time import monotonic, time
from threading import Lock

_path = None
_init_lock = Lock()
_scope = ContextVar('usage_scope', default={})


def configure(path):
    global _path
    _path = Path(path) if path else None


@contextmanager
def scope(user_id=None, context=None, feature=None):
    values = dict(_scope.get())
    values.update({key:value for key, value in [('user_id', user_id), ('context', context), ('feature', feature)] if value is not None})
    token = _scope.set(values)
    try:
        yield
    finally:
        _scope.reset(token)


def connect():
    if _path is None:
        return None
    with _init_lock:
        _path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(_path), timeout=5)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY, at REAL NOT NULL, category TEXT NOT NULL, action TEXT NOT NULL,
            status TEXT NOT NULL, seconds REAL NOT NULL, bytes INTEGER NOT NULL, user_id INTEGER,
            context TEXT NOT NULL, feature TEXT NOT NULL, model TEXT NOT NULL,
            prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER)''')
        db.execute('CREATE INDEX IF NOT EXISTS usage_at ON usage_events(at)')
    return db


def record(category, action, status, seconds=0, byte_count=0, model='', usage=None):
    if _path is None:
        return
    db = None
    try:
        db = connect()
        values, usage = _scope.get(), usage if isinstance(usage, dict) else {}
        tokens = [usage.get(key) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        tokens = [value if type(value) is int and 0 <= value < 2 ** 63 else None for value in tokens]
        user_id = values.get('user_id')
        with db:
            db.execute('''INSERT INTO usage_events (at,category,action,status,seconds,bytes,user_id,context,feature,
                          model,prompt_tokens,completion_tokens,total_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (time(), category, action, status, max(0, seconds), max(0, byte_count),
                        user_id if type(user_id) is int and user_id > 0 else None,
                        values.get('context', 'background'), values.get('feature', ''), str(model)[:160], *tokens))
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        print('Usage metrics unavailable (' + type(error).__name__ + ').', file=sys.stderr)
    finally:
        if db is not None:
            db.close()


def track_message(context):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            message = args[1].get('message', {}) if context == 'private' else args[1]
            if context == 'private' and message.get('chat', {}).get('type') != 'private':
                return function(*args, **kwargs)
            started, status = monotonic(), 'handled'
            with scope((message.get('from') or {}).get('id'), context):
                try:
                    return function(*args, **kwargs)
                except Exception:
                    status = 'exception'; raise
                finally:
                    record('message', 'request', status, monotonic() - started)
        return wrapped
    return decorate


def track_llm(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        started, status, result = monotonic(), 'error', None
        model = args[1] if len(args) > 1 else kwargs.get('model', '')
        try:
            result = function(*args, **kwargs); status = 'success'; return result
        finally:
            usage = result.get('usage', {}) if isinstance(result, dict) else {}
            if isinstance(result, dict) and not usage and 'eval_count' in result:
                prompt, completion = result.get('prompt_eval_count', 0), result['eval_count']
                if type(prompt) is int and type(completion) is int:
                    usage = {'prompt_tokens':prompt, 'completion_tokens':completion, 'total_tokens':prompt + completion}
            record('llm', 'completion', status, monotonic() - started, model=model, usage=usage)
    return wrapped


def rows(days=7):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('Період статистики — від 1 до 3650 днів.')
    db = connect()
    if db is None:
        return []
    try:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM usage_events WHERE at>=? ORDER BY id', (time() - days * 86400,))]
    finally:
        db.close()


def summary(days=7):
    events = rows(days)
    def aggregate(values):
        times = sorted(event['seconds'] for event in values)
        success = sum(event['status'] == 'success' for event in values)
        return {'count':len(values), 'success':success, 'errors':sum(event['status'] in {'error', 'exception', 'rejected'} for event in values),
                'success_percent':round(100 * success / len(values), 1) if values else None,
                'mean_seconds':round(sum(times) / len(times), 3) if times else None,
                'p95_seconds':round(times[math.ceil(len(times) * .95) - 1], 3) if times else None,
                'bytes':sum(event['bytes'] for event in values)}
    groups = {}
    for event in events:
        key = event['category'] + '/' + event['action']
        groups.setdefault(key, []).append(event)
    llm = [event for event in events if event['category'] == 'llm']
    known = [event for event in llm if event['total_tokens'] is not None]
    return {'days':days, 'events':len(events), 'unique_users':len({event['user_id'] for event in events if event['user_id']}),
            'groups':{key:aggregate(values) for key, values in groups.items()},
            'llm_requests':len(llm), 'llm_with_known_tokens':len(known), 'known_total_tokens':sum(event['total_tokens'] for event in known)}


def report(days=7):
    data = summary(days)
    lines = ['Статистика за ' + str(days) + ' дн.', 'Користувачів: ' + str(data['unique_users'])]
    for key, group in data['groups'].items():
        if key.startswith('message/'):
            lines.append('Оброблено повідомлень: ' + str(group['count']) + ' · винятків: ' + str(group['errors']))
        else:
            lines.append(key + ': ' + str(group['count']) + ' · успішно ' + str(group['success']) +
                         ' (' + str(group['success_percent']) + '%) · середнє ' + str(group['mean_seconds']) +
                         ' с · p95 ' + str(group['p95_seconds']) + ' с' +
                         (' · ' + str(group['bytes']) + ' байт' if group['bytes'] else ''))
    lines += ['ШІ: ' + str(data['llm_requests']) + ' викликів; відомих токенів: ' + str(data['known_total_tokens']) +
              ' (usage є у ' + str(data['llm_with_known_tokens']) + ' викликів).',
              'Це вимірювання роботи, не оцінка якості ШІ чи зекономленого часу.', '/stats csv — події для звіту.']
    return '\n'.join(lines)


def export_csv(days=7):
    output = io.StringIO(newline='')
    fields = ['id','at','category','action','status','seconds','bytes','user_id','context','feature','model','prompt_tokens','completion_tokens','total_tokens']
    writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader(); writer.writerows(rows(days))
    return ('\ufeff' + output.getvalue()).encode('utf-8')
