from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import file_commands as commands
from file_store import FileStore, FileError
from file_paths import catalog, folder_id, resolve
from guest_bot import GuestAnswers
from telegram_bot import Telegram, format_chat_html
import usage_metrics as metrics


class StorageCommandsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {'FILES_ENABLED':'1', 'FILES_ALLOWED_USER_IDS':'101', 'FILES_TELEGRAM_CACHE_CHAT_ID':'-100777'}
        self.store = FileStore(Path(self.temp.name) / 'files', config=lambda:self.config)
        metrics.configure(Path(self.temp.name) / 'usage.sqlite3')
        self.api = Mock(file_cache_key='bot-key')
        self.api.download_file.return_value = b'hello'
        self.api.send_file.return_value = {'message_id':99, 'document':{'file_id':'uploaded-file'}}
        self.api.call.side_effect = lambda method, **params: {'type':'channel'} if method == 'getChat' else True
        self.reply = Mock()

    def tearDown(self):
        metrics.configure(None)
        self.temp.cleanup()

    def message(self, text, guest=True):
        value = {'message_id':7, 'from':{'id':101}, 'chat':{'id':999, 'type':'private'}, 'text':text}
        if guest:
            value['guest_query_id'] = 'query-1'
        return value

    def save_message(self, text='збережи в /Навчання/Лаби/'):
        value = self.message(text)
        value['reply_to_message'] = {'from':{'id':202}, 'document':{'file_id':'original-file', 'file_name':'звіт.txt', 'file_size':5}}
        return value

    def run_command(self, message):
        return commands.respond(self.api, message, 'model', self.reply, store=self.store)

    def test_reply_save_creates_nested_folders_and_guest_send_reuses_cached_document(self):
        self.assertTrue(self.run_command(self.save_message()))
        item = catalog(self.store, 101)[0]
        self.assertEqual(item['path'], '/Навчання/Лаби/звіт.txt')
        self.assertEqual(self.store.get_file(101, item['id'])[0].read_bytes(), b'hello')
        self.run_command(self.message('надішли ' + item['path']))
        self.api.send_file.assert_not_called()
        self.api.send.assert_not_called()
        result = self.api.call.call_args.kwargs
        self.assertNotIn('chat_id', result)
        self.assertEqual(result['guest_query_id'], 'query-1')
        self.assertEqual(result['result']['document_file_id'], 'original-file')
        self.assertEqual(metrics.summary()['groups']['files/збережи']['bytes'], 5)

    def test_same_guest_update_is_idempotent_but_another_message_gets_suffix(self):
        value = self.save_message()
        self.run_command(value); self.run_command(value)
        self.assertEqual(len(self.store.list_files(101)), 1)
        value['guest_query_id'] = 'query-2'
        self.run_command(value)
        self.assertEqual({item['name'] for item in self.store.list_files(101)}, {'звіт.txt', 'звіт (2).txt'})

    def test_save_as_new_name_requires_upload_cache_and_sends_to_guest_chat(self):
        self.run_command(self.save_message('збережи як /Notes/new.txt'))
        item = catalog(self.store, 101)[0]
        self.assertIsNone(self.store.telegram_cached(101, item['id'], 'bot-key'))
        self.run_command(self.message('надішли #' + str(item['id'])))
        self.api.send_file.assert_called_once()
        self.assertEqual(self.api.send_file.call_args.args[0], '-100777')
        self.assertEqual(self.api.call.call_args.kwargs['result']['document_file_id'], 'uploaded-file')

    def test_channel_id_helper_accepts_only_forwarded_channel_and_authorized_private_caller(self):
        message = self.message('/channelid', guest=False)
        message['reply_to_message'] = {'forward_origin':{'type':'channel', 'chat':{'id':-100777, 'type':'channel', 'title':'Storage cache'}}}
        self.run_command(message)
        self.assertIn('FILES_TELEGRAM_CACHE_CHAT_ID=-100777', self.reply.call_args.args[1])
        message['guest_query_id'] = 'guest'
        self.run_command(message)
        self.assertIn('особистому', self.reply.call_args.args[1])
        self.api.send_file.assert_not_called()

    def test_unconfigured_staging_never_sends_file_to_guessed_private_chat(self):
        self.config.pop('FILES_TELEGRAM_CACHE_CHAT_ID')
        file_id = self.store.upload(101, 'web.txt', b'web')
        self.run_command(self.message('надішли #' + str(file_id)))
        self.api.send_file.assert_not_called(); self.api.call.assert_not_called()
        self.assertIn('CACHE_CHAT_ID', self.reply.call_args.args[1])

    def test_editing_and_renaming_invalidates_old_telegram_cache(self):
        file_id = self.store.upload(101, 'web.txt', b'old')
        self.store.telegram_remember(101, file_id, 'bot-key', 'stale', 'document')
        self.assertIsNone(self.store.telegram_cached(101, file_id, 'another-bot'))
        revision = self.store.text_preview(101, file_id)['revision']
        self.store.replace_text(101, file_id, 'new content', revision)
        self.assertIsNone(self.store.telegram_cached(101, file_id, 'bot-key'))
        self.run_command(self.message('надішли #' + str(file_id)))
        self.assertEqual(self.api.send_file.call_args.args[1].read_bytes(), b'new content')
        self.store.update_item(101, 'file', file_id, 'renamed.txt')
        self.assertIsNone(self.store.telegram_cached(101, file_id, 'bot-key'))

    def test_edit_during_staging_does_not_cache_old_bytes_as_new_version(self):
        file_id = self.store.upload(101, 'web.txt', b'old')
        def upload(*args, **kwargs):
            revision = self.store.text_preview(101, file_id)['revision']
            self.store.replace_text(101, file_id, 'newer', revision)
            return {'message_id':99, 'document':{'file_id':'old-upload'}}
        self.api.send_file.side_effect = upload
        self.run_command(self.message('надішли #' + str(file_id)))
        self.assertIsNone(self.store.telegram_cached(101, file_id, 'bot-key'))
        self.assertFalse(any(call.args[0] == 'answerGuestQuery' for call in self.api.call.call_args_list))
        self.assertIn('змінився', self.reply.call_args.args[1])

    def test_stats_csv_has_events_without_file_names_and_guest_does_not_publish_export(self):
        self.run_command(self.save_message())
        self.run_command(self.message('/stats csv 30', guest=False))
        self.api.send_bytes.assert_called_once()
        raw = self.api.send_bytes.call_args.args[2]
        self.assertIn(b'category', raw)
        self.assertNotIn('звіт.txt'.encode(), raw)
        self.api.send_bytes.reset_mock()
        self.run_command(self.message('/stats csv 30'))
        self.api.send_bytes.assert_not_called()
        self.assertIn('особистому', self.reply.call_args.args[1])

    def test_expired_guest_answer_never_falls_back_to_another_chat(self):
        file_id = self.store.upload(101, 'web.txt', b'web')
        self.store.telegram_remember(101, file_id, 'bot-key', 'cached', 'document')
        self.api.call.side_effect = OSError('expired')
        self.run_command(self.message('надішли #' + str(file_id)))
        self.api.send_file.assert_not_called(); self.api.send.assert_not_called()
        self.assertEqual(metrics.summary()['groups']['files/надішли']['errors'], 1)

    def test_staging_refuses_public_channels_groups_and_positive_private_chat_ids(self):
        file_id = self.store.upload(101, 'web.txt', b'web')
        for response in ({'type':'channel', 'username':'public'}, {'type':'supergroup'}):
            self.api.call.side_effect = lambda *args, **kwargs: response
            self.run_command(self.message('надішли #' + str(file_id)))
            self.api.send_file.assert_not_called()
        self.config['FILES_TELEGRAM_CACHE_CHAT_ID'] = '101'
        self.api.call.reset_mock()
        self.run_command(self.message('надішли #' + str(file_id)))
        self.api.call.assert_not_called(); self.api.send_file.assert_not_called()

    def test_unauthorized_caller_cannot_save_search_send_or_see_metrics(self):
        for text in ('збережи в /Private/', 'знайди через ші секрет', 'надішли #1', '/stats'):
            value = self.save_message(text); value['from']['id'] = 202
            with patch.object(commands, 'chat') as llm, patch.object(self.store, 'connect') as database:
                self.run_command(value)
                llm.assert_not_called(); database.assert_not_called()
        self.api.download_file.assert_not_called(); self.api.send_file.assert_not_called()
        self.assertEqual(metrics.summary()['groups']['files/надішли']['errors'], 1)

    def test_oversized_and_missing_or_protected_attachment_never_download(self):
        value = self.save_message(); value['reply_to_message']['document']['file_size'] = commands.MAX_DOWNLOAD_BYTES + 1
        self.run_command(value)
        value['reply_to_message']['has_protected_content'] = True
        self.run_command(value)
        self.run_command(self.message('збережи в /Notes/'))
        self.api.download_file.assert_not_called()
        self.assertEqual(self.store.list_files(101), [])

    def test_logical_paths_reject_traversal_and_duplicate_names_require_id(self):
        for path in ('../outside', '/a/../outside', '/a\\b', '/a//b'):
            with self.assertRaises(FileError):
                folder_id(self.store, 101, path, create=True)
        first = folder_id(self.store, 101, '/One/', create=True)
        second = folder_id(self.store, 101, '/Two/', create=True)
        self.store.upload(101, 'same.txt', b'one', first)
        file_id = self.store.upload(101, 'same.txt', b'two', second)
        self.run_command(self.message('надішли same.txt'))
        self.assertIn('кілька', self.reply.call_args.args[1])
        self.api.call.assert_not_called()
        self.assertEqual(resolve(self.store, 101, '/Two/same.txt')[0]['id'], file_id)

    def test_ai_search_filters_hallucinated_ids_and_does_not_send_files(self):
        file_id = self.store.upload(101, 'routing-lab.txt', b'private contents never sent to model')
        with patch.object(commands, 'chat', return_value={'message':{'content':json.dumps({'ids':[999999, True, file_id, file_id]})}}) as model:
            items, note = commands.search_files(self.store, 101, 'через ші лаба про мережі', 'model')
        self.assertEqual([item['id'] for item in items], [file_id])
        self.assertIn('без читання вмісту', note)
        self.assertNotIn('private contents', str(model.call_args))
        self.api.call.assert_not_called()

    def test_private_file_send_uses_current_chat_and_preserves_reply_topic(self):
        file_id = self.store.upload(101, 'web.txt', b'web')
        message = self.message('надішли #' + str(file_id), guest=False); message['message_thread_id'] = 55
        self.run_command(message)
        self.assertEqual(self.api.send_file.call_args.args[0], 999)
        self.assertEqual(self.api.send_file.call_args.kwargs['message_thread_id'], 55)
        self.assertEqual(self.api.send_file.call_args.kwargs['reply_parameters']['message_id'], 7)

    def test_guest_file_command_precedes_voice_transcription(self):
        worker = GuestAnswers(self.api, 'model', format_chat_html, generate=Mock())
        message = self.message('@DBresh_bot збережи в /Voice/')
        message['reply_to_message'] = {'voice':{'file_id':'voice-id', 'duration':6}}
        with patch.object(commands, 'FileStore', return_value=self.store):
            worker.process(message)
        self.api.download_voice.assert_not_called(); worker.generate.assert_not_called()
        self.assertEqual(len(self.store.list_files(101)), 1)

    def test_general_chat_summary_prompt_is_not_intercepted(self):
        self.assertFalse(self.run_command(self.message('підсумуй цей текст будь ласка')))
        self.assertFalse(self.run_command(self.message('надішли привіт')))


class FileTransportTests(unittest.TestCase):
    def test_multipart_stream_preserves_bytes_and_does_not_read_entire_file(self):
        api = Telegram('123:test')
        class Source(BytesIO):
            def read(self, count=-1):
                if not 0 < count <= 256 * 1024:
                    raise AssertionError('must stream')
                return super().read(count)
        data = b'\x00\xff' * 200000
        def request(method, body, mime, timeout, length=None):
            joined = b''.join(body)
            self.assertEqual(len(joined), length)
            self.assertIn(data, joined)
            self.assertIn('звіт.txt'.encode(), joined)
            return {'document':{'file_id':'cached'}}
        with patch.object(api, 'request', side_effect=request):
            result = api._send_file_stream(101, 'звіт.txt', Source(data), len(data), 'application/octet-stream', {})
        self.assertEqual(result['document']['file_id'], 'cached')

    def test_download_rejects_unsafe_path_and_bounds_actual_body(self):
        api = Telegram('123:test')
        for path in ('../secret', '/etc/passwd', 'https://evil.test/a', 'a\\b'):
            with patch.object(api, 'call', return_value={'file_path':path}), patch('telegram_bot.urlopen') as network:
                with self.assertRaises(FileError):
                    api.download_file('file', 5)
                network.assert_not_called()
        response = BytesIO(b'123456')
        with patch.object(api, 'call', return_value={'file_path':'documents/a.bin'}), patch('telegram_bot.urlopen', return_value=response):
            with self.assertRaises(FileError):
                api.download_file('file', 5)
