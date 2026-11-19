from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from file_store import FileStore, FileError, FileConflict
from file_web import FileApp


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config={'FILES_ENABLED':'1','FILES_ALLOWED_USER_IDS':'101','FILES_PUBLIC_URL':'https://files.example.org'}
        self.store=FileStore(Path(self.temp.name)/'store',config=lambda:self.config)

    def tearDown(self):self.temp.cleanup()

    def test_file_delete_removes_bytes_metadata_and_telegram_references(self):
        fid=self.store.upload_stream(101,'test.txt',BytesIO(b'hello'),5,request_key='test-request')
        path,_,_=self.store.get_file(101,fid)
        self.store.telegram_remember(101,fid,'bot','cached-telegram-file','document')
        result=self.store.delete_item(101,'file',fid,'test.txt')
        self.assertEqual(result,{'files':1,'folders':0,'bytes':5,'cleanup_pending':0})
        self.assertFalse(path.exists())
        with self.assertRaises(FileError):self.store.get_file(101,fid)
        db=self.store.connect()
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM telegram_saves').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM telegram_files').fetchone()[0],0)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:db.close()

    def test_folder_delete_removes_only_selected_subtree(self):
        root=self.store.create_folder(101,'root')
        nested=self.store.create_folder(101,'nested',root)
        sibling=self.store.create_folder(101,'keep')
        self.store.upload(101,'a.txt',b'a',root)
        self.store.upload(101,'b.txt',b'bb',nested)
        keep=self.store.upload(101,'keep.txt',b'keep',sibling)
        result=self.store.delete_item(101,'folder',root,'root')
        self.assertEqual(result['files'],2);self.assertEqual(result['folders'],2)
        self.assertEqual([f['id'] for f in self.store.list_folders(101)],[sibling])
        self.assertEqual(self.store.get_file(101,keep)[0].read_bytes(),b'keep')
        self.assertEqual(len(list(self.store.blobs.iterdir())),1)

    def test_ids_never_reused_after_deleting_last_item(self):
        first=self.store.upload(101,'old.txt',b'old')
        folder=self.store.create_folder(101,'old')
        self.store.delete_item(101,'file',first,'old.txt')
        self.store.delete_item(101,'folder',folder,'old')
        self.assertGreater(self.store.upload(101,'new.txt',b'new'),first)
        self.assertGreater(self.store.create_folder(101,'new'),folder)

    def test_renamed_item_and_revoked_user_cannot_delete(self):
        fid=self.store.upload(101,'test.txt',b'hello')
        with self.assertRaises(FileConflict):self.store.delete_item(101,'file',fid,'old-name.txt')
        self.config['FILES_ALLOWED_USER_IDS']='202'
        with self.assertRaises(FileError):self.store.delete_item(101,'file',fid,'test.txt')
        self.config['FILES_ALLOWED_USER_IDS']='101'
        self.assertEqual(self.store.get_file(101,fid)[0].read_bytes(),b'hello')

    def test_database_failure_restores_staged_blobs_and_metadata(self):
        fid=self.store.upload(101,'test.txt',b'hello')
        db=self.store.connect()
        db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON files BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        db.commit();db.close()
        with self.assertRaises(sqlite3.IntegrityError):self.store.delete_item(101,'file',fid,'test.txt')
        self.assertEqual(self.store.get_file(101,fid)[0].read_bytes(),b'hello')
        self.assertEqual(len(list(self.store.blobs.iterdir())),1)

    def test_invalid_blob_cannot_touch_path_outside_storage(self):
        fid=self.store.upload(101,'test.txt',b'hello')
        outside=Path(self.temp.name)/'keep.txt';outside.write_bytes(b'keep')
        db=self.store.connect();db.execute('UPDATE files SET blob=? WHERE id=?',('../keep.txt',fid));db.commit();db.close()
        with self.assertRaises(FileError):self.store.delete_item(101,'file',fid,'test.txt')
        self.assertEqual(outside.read_bytes(),b'keep')

    def test_api_requires_session_origin_csrf_and_explicit_confirmation(self):
        fid=self.store.upload(101,'test.txt',b'hello');app=FileApp(self.store)
        def request(body,origin='https://files.example.org',csrf='csrf'):
            data=json.dumps(body).encode();result={}
            env={'REQUEST_METHOD':'POST','PATH_INFO':'/api/files/'+str(fid)+'/delete','HTTP_HOST':'files.example.org','HTTP_ORIGIN':origin,
                 'HTTP_COOKIE':'files_session=test','HTTP_X_CSRF_TOKEN':csrf,'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(data)),'wsgi.input':BytesIO(data)}
            result['body']=b''.join(app(env,lambda status,headers:result.update(status=status)))
            return result
        body={'name':'test.txt','confirmed':True}
        self.assertEqual(request(body)['status'],'403 Forbidden')
        with patch.object(self.store,'session',return_value=(101,'csrf')):
            self.assertEqual(request(body,csrf='')['status'],'403 Forbidden')
            self.assertEqual(request(body,origin='https://evil.example')['status'],'403 Forbidden')
            self.assertEqual(request({'name':'test.txt'})['status'],'403 Forbidden')
            self.assertEqual(request(body)['status'],'200 OK')
        with self.assertRaises(FileError):self.store.get_file(101,fid)

if __name__=='__main__':unittest.main()
