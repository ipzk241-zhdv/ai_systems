import csv
import io
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import settings
import usage_metrics as metrics


class UsageMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        metrics.configure(Path(self.temp.name) / 'usage.sqlite3')

    def tearDown(self):
        metrics.configure(None)
        self.temp.cleanup()

    def test_success_rate_latency_percentile_bytes_and_unknown_tokens(self):
        for seconds in range(1, 101):
            with metrics.scope(user_id=101, context='guest'):
                metrics.record('files', 'save', 'success' if seconds <= 90 else 'error', seconds, byte_count=10)
        metrics.record('llm', 'completion', 'success', usage={'total_tokens':12})
        metrics.record('llm', 'completion', 'error')
        data = metrics.summary(7)
        saved = data['groups']['files/save']
        self.assertEqual(saved['count'], 100)
        self.assertEqual(saved['success_percent'], 90)
        self.assertEqual(saved['mean_seconds'], 50.5)
        self.assertEqual(saved['p95_seconds'], 95)
        self.assertEqual(saved['bytes'], 1000)
        self.assertEqual(data['unique_users'], 1)
        self.assertEqual(data['known_total_tokens'], 12)
        self.assertEqual(data['llm_with_known_tokens'], 1)
        self.assertEqual(data['llm_requests'], 2)

    def test_llm_measurement_preserves_output_errors_and_does_not_store_prompts(self):
        @metrics.track_llm
        def generate(messages, model):
            return {'message':{'content':'private answer'}, 'usage':{'total_tokens':42}}
        with metrics.scope(user_id=101, feature='file_search'):
            result = generate([{'content':'private prompt /Secrets/password.txt'}], 'model')
        self.assertEqual(result['message']['content'], 'private answer')
        events = metrics.rows()
        self.assertEqual(events[0]['total_tokens'], 42)
        self.assertEqual(events[0]['feature'], 'file_search')
        self.assertNotIn('private', str(events))
        self.assertNotIn('/Secrets', metrics.export_csv().decode('utf-8-sig'))
        @metrics.track_llm
        def failed(messages, model):
            raise ValueError('secret provider body')
        with self.assertRaises(ValueError):
            failed([], 'model')
        self.assertEqual(metrics.rows()[-1]['status'], 'error')
        self.assertNotIn('secret provider body', str(metrics.rows()))

    def test_metrics_storage_failure_does_not_break_llm_result(self):
        @metrics.track_llm
        def generate(messages, model):
            return {'message':{'content':'answer'}}
        with patch.object(metrics, 'connect', side_effect=OSError('read-only disk')):
            self.assertEqual(generate([], 'model')['message']['content'], 'answer')

    def test_thread_local_scopes_and_csv_export(self):
        def record(user_id):
            with metrics.scope(user_id=user_id, context='guest'):
                metrics.record('files', 'send', 'success')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(record, [101, 202]))
        self.assertEqual(metrics.summary()['unique_users'], 2)
        rows = list(csv.DictReader(io.StringIO(metrics.export_csv().decode('utf-8-sig'))))
        self.assertEqual({row['user_id'] for row in rows}, {'101', '202'})
        self.assertEqual({row['context'] for row in rows}, {'guest'})

    def test_new_env_settings_are_actually_read(self):
        path = Path(self.temp.name) / '.env'
        path.write_text('FILES_MAX_UPLOAD_ITEMS=20000\nFILES_TELEGRAM_CACHE_CHAT_ID=-100777\n', encoding='utf-8')
        with patch.dict('os.environ', {}, clear=True):
            value = settings.read_config(path)
        self.assertEqual(value['FILES_MAX_UPLOAD_ITEMS'], '20000')
        self.assertEqual(value['FILES_TELEGRAM_CACHE_CHAT_ID'], '-100777')

    def test_invalid_report_period_rejected(self):
        for days in (0, -1, 3651, '7', True):
            with self.assertRaises(ValueError):
                metrics.summary(days)
