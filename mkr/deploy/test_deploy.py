import io
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import deploy_vm


class DeployTest(unittest.TestCase):
    def test_ssh_command_is_restricted(self):
        sha = "a" * 40
        self.assertEqual(deploy_vm.parse_command("deploy " + sha), sha)
        for command in ("bash", "deploy master", "deploy " + sha + "; id", "deploy " + sha + "\n", ""):
            with self.assertRaises(ValueError): deploy_vm.parse_command(command)

    def archive(self, path, kind=None):
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w") as archive:
            item = tarfile.TarInfo(path)
            if kind:
                item.type = kind
                item.linkname = "/etc/passwd"
                archive.addfile(item)
            else:
                item.size = 4
                archive.addfile(item, io.BytesIO(b"test"))
        return result.getvalue()

    def test_archive_rejects_secrets_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deploy_vm.extract_code(self.archive("mkr/solution/bot.py"), root)
            self.assertEqual((root / "mkr/solution/bot.py").read_text(), "test")
            for path in ("/etc/passwd", "mkr/../../escape", "mkr/solution/.env", "mkr/solution/.runtime/bot.sqlite3", "other/a.py", "mkr/solution/__pycache__/bot.pyc"):
                with self.assertRaises(ValueError): deploy_vm.extract_code(self.archive(path), root)
            with self.assertRaises(ValueError):
                deploy_vm.extract_code(self.archive("mkr/link", tarfile.SYMTYPE), root)

    def test_database_snapshot_is_valid_and_does_not_modify_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            source = runtime / "bot-123.sqlite3"
            with closing(sqlite3.connect(str(source))) as db:
                db.execute("CREATE TABLE checks (value TEXT)")
                db.execute("INSERT INTO checks VALUES ('preserved')")
                db.commit()
            backup = root / "snapshot"
            deploy_vm.snapshot(runtime, backup)
            with closing(sqlite3.connect(str(backup / source.name))) as db:
                self.assertEqual(db.execute("SELECT value FROM checks").fetchone()[0], "preserved")

    def test_dirty_checkout_is_not_overwritten(self):
        with patch.object(deploy_vm, "run", return_value=" M mkr/solution/bot.py"):
            with self.assertRaises(ValueError): deploy_vm.ensure_clean(Path("repo"))

    def test_stable_pid_is_required(self):
        with patch.object(deploy_vm, "time") as timer, patch.object(deploy_vm, "run", side_effect=["100", "active", "101"]):
            with self.assertRaises(ValueError): deploy_vm.healthy("news-bot", checks=1)

    def test_failure_restores_previous_code(self):
        sha, old = "a" * 40, "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mkr/solution").mkdir(parents=True)
            (root / "mkr/solution/.env").write_text("PRIVATE", encoding="utf-8")
            calls = []
            def run(args, cwd=None):
                calls.append(args)
                if args[:2] == ["git", "rev-parse"]:
                    return sha if args[-1] != "HEAD" else old
                return ""
            with patch.object(deploy_vm, "ensure_clean"), patch.object(deploy_vm, "run", side_effect=run), patch.object(deploy_vm, "extract_code"), patch.object(deploy_vm, "healthy", side_effect=ValueError("failed")), patch.object(deploy_vm.subprocess, "run"):
                with self.assertRaises(ValueError): deploy_vm.deploy(sha, root, root / "state")
            self.assertIn(["git", "reset", "--hard", old], calls)
            self.assertEqual((root / "mkr/solution/.env").read_text(), "PRIVATE")


if __name__ == "__main__":
    unittest.main()
