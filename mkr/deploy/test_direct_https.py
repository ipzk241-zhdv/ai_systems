from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import direct_files_https as https


class DirectHttpsTest(unittest.TestCase):
    def test_public_http_serves_only_challenges_and_redirects_other_paths(self):
        config=https.nginx_site('141.147.21.157',8080,100)
        self.assertIn('listen 80;',config)
        self.assertIn('location ^~ /.well-known/acme-challenge/',config)
        self.assertIn('return 301 https://141.147.21.157$request_uri',config)
        self.assertNotIn('proxy_pass',config)

    def test_https_proxy_uses_certificate_loopback_and_configured_size(self):
        config=https.nginx_site('141.147.21.157',8088,200,tls=True)
        self.assertIn('listen 443 ssl;',config)
        self.assertIn('ssl_protocols TLSv1.2 TLSv1.3',config)
        self.assertIn('proxy_pass http://127.0.0.1:8088',config)
        self.assertIn('client_max_body_size 200m;',config)
        self.assertIn('proxy_set_header Host $host;',config)
        with self.assertRaises(ValueError):
            https.nginx_site('bad; configuration',8080,100)

    def test_proxy_config_accepts_two_gib_file_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            path.write_text('FILES_ENABLED=1\nFILES_PORT=8080\nFILES_MAX_FILE_MB=2048\n',encoding='utf-8')
            with patch.object(https,'ENV',path):
                self.assertEqual(https.config(),(8080,2048))
        self.assertIn('client_max_body_size 2048m;',https.nginx_site('141.147.21.157',8080,2048,tls=True))

    def test_public_url_update_preserves_other_config_and_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            original='TOKEN=secret\nFILES_PUBLIC_URL=old\nFILES_ENABLED=1\nFILES_PUBLIC_URL=duplicate\n'
            path.write_text(original,encoding='utf-8')
            owner=path.stat().st_uid
            with patch.object(https,'ENV',path):
                https.update_url('141.147.21.157')
            content=path.read_text(encoding='utf-8')
            self.assertIn('TOKEN=secret\n',content)
            self.assertIn('FILES_ENABLED=1\n',content)
            self.assertEqual(content.count('FILES_PUBLIC_URL='),1)
            self.assertIn('FILES_PUBLIC_URL=https://141.147.21.157\n',content)
            self.assertEqual(path.stat().st_uid,owner)

    def test_failed_env_replacement_keeps_original_and_removes_temporary_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            path.write_text('TOKEN=secret',encoding='utf-8')
            with patch.object(https,'ENV',path),patch.object(https.os,'replace',side_effect=OSError('blocked')):
                with self.assertRaises(OSError):
                    https.update_url('141.147.21.157')
            self.assertEqual(path.read_text(encoding='utf-8'),'TOKEN=secret')
            self.assertEqual(list(Path(directory).iterdir()),[path])


if __name__=='__main__':
    unittest.main()
