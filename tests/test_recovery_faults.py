"""Synthetic checkpoint faults and real executor exit at a task boundary."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reproforge import runner, storage
from reproforge.locking import project_lock
from reproforge.spec import load_plan
from reproforge.storage import StorageError, atomic_json


def make_plan(root):
    tasks = [{"id": "first", "command": ["@python", "-c",
              "from pathlib import Path; Path('one.txt').write_text('synthetic')"],
              "outputs": ["one.txt"], "cache": True},
             {"id": "second", "command": ["@python", "-c",
              "from pathlib import Path; Path('two.txt').write_text(Path('one.txt').read_text()+'-done')"],
              "inputs": [{"task": "first", "artifact": "one.txt", "as": "one.txt"}],
              "outputs": ["two.txt"]}]
    path = root / "project.json"
    path.write_text(json.dumps({"version": 1, "tasks": tasks}), encoding="utf-8")
    return load_plan(path)


class CheckpointFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "ledger.json"
        self.old = b'{"status":"running","checkpoint":1}\n'
        self.path.write_bytes(self.old)

    def assert_old_intact(self):
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["ledger.json"])

    def test_sync_failure_preserves_old_checkpoint(self):
        with patch.object(storage.os, "fsync", side_effect=OSError(errno.EIO, "synthetic sync failure")):
            with self.assertRaises(StorageError):
                atomic_json(self.path, {"status": "success"})
        self.assert_old_intact()

    def test_sync_failure_never_creates_initial_checkpoint(self):
        self.path.unlink()
        with patch.object(storage.os, "fsync", side_effect=OSError(errno.ENOSPC, "synthetic full")):
            with self.assertRaises(StorageError):
                atomic_json(self.path, {"status": "running"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_complete_flushed_bytes_synced_before_replace(self):
        real_fsync, real_replace = os.fsync, os.replace
        events = []
        new = {"status": "success", "text": "synthetic 中文"}

        def synced(fd):
            # The temporary bytes must already be flushed and complete.
            with next(self.root.glob(".tmp-*")).open(encoding="utf-8") as stream:
                self.assertEqual(json.load(stream), new)
            self.assertEqual(self.path.read_bytes(), self.old)
            real_fsync(fd)
            events.append("sync")

        def replace(source, destination):
            self.assertEqual(events, ["sync"])
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), new)
            real_replace(source, destination)
            events.append("publish")

        with patch.object(storage.os, "fsync", side_effect=synced), patch.object(storage.os, "replace", side_effect=replace):
            atomic_json(self.path, new)
        self.assertEqual(events, ["sync", "publish"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), new)

    def test_partial_serialization_keeps_previous_checkpoint(self):
        def interrupted(value, stream, **options):
            stream.write('{"status":')
            raise OSError(errno.ENOSPC, "synthetic partial write")
        with patch.object(storage.json, "dump", side_effect=interrupted):
            with self.assertRaises(StorageError):
                atomic_json(self.path, {"status": "success"})
        self.assert_old_intact()

    def test_replace_failure_keeps_previous_checkpoint(self):
        with patch.object(storage.os, "replace", side_effect=OSError(errno.EACCES, "synthetic publish denial")):
            with self.assertRaises(StorageError):
                atomic_json(self.path, {"status": "success"})
        self.assert_old_intact()

    def test_nonfinite_report_never_replaces_previous_checkpoint(self):
        with self.assertRaises(StorageError):
            atomic_json(self.path, {"value": float("nan")})
        self.assert_old_intact()


class RecoveryFaultTests(unittest.TestCase):
    def test_final_checkpoint_failure_creates_no_cache_then_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = make_plan(root)
            real_atomic = runner.atomic_json

            def fail_final(path, value):
                if value.get("status") == "success":
                    raise StorageError("synthetic final checkpoint failure")
                return real_atomic(path, value)

            with patch.object(runner, "atomic_json", side_effect=fail_final):
                with self.assertRaisesRegex(StorageError, "checkpoint"):
                    runner.execute(plan, reuse=True)
            store = storage.Store(root / ".reproforge")
            ledger = next(store.runs.glob("*.json"))
            record = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "running")
            self.assertEqual(len(record["tasks"]), 2)
            self.assertEqual(list(store.cache.glob("*.json")), [])
            self.assertEqual(runner.recover(plan), [record["run_id"]])
            original = ledger.read_bytes()
            self.assertEqual(runner.verify(plan, record["run_id"]), [])
            resumed = runner.execute(plan, resume=record["run_id"])
            self.assertEqual(resumed["status"], "success")
            self.assertTrue(all(task["resumed_from"] == record["run_id"] for task in resumed["tasks"]))
            self.assertEqual(runner.verify(plan, resumed["run_id"]), [])
            self.assertEqual(ledger.read_bytes(), original)

    def test_actual_executor_exit_releases_lock_and_resumes_verified_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = make_plan(root)
            # Exit abruptly only once first task's checkpoint has published.
            # No task process is active here; do not claim child-tree cleanup.
            script = """
import os, sys
from pathlib import Path
from reproforge import runner
from reproforge.spec import load_plan
original = runner.atomic_json
def checkpoint(path, value):
    original(path, value)
    if value.get('status') == 'running' and len(value.get('tasks', [])) == 1:
        os._exit(73)
runner.atomic_json = checkpoint
runner.execute(load_plan(Path(sys.argv[1])))
"""
            exited = subprocess.run([sys.executable, "-c", script, str(plan.path)],
                                    capture_output=True, timeout=30)
            self.assertEqual(exited.returncode, 73, exited.stderr)
            store = storage.Store(root / ".reproforge")
            ledger = next(store.runs.glob("*.json"))
            record = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "running")
            self.assertEqual([task["id"] for task in record["tasks"]], ["first"])
            self.assertEqual(list(store.cache.glob("*.json")), [])
            with project_lock(store.root, timeout=0.2):
                pass  # OS lock release after abrupt subprocess death
            recovered = subprocess.run([sys.executable, "-m", "reproforge", "recover", str(plan.path)],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(record["run_id"], recovered.stdout)
            self.assertEqual(runner.recover(plan), [])  # idempotent
            source_bytes = ledger.read_bytes()
            self.assertEqual(runner.verify(plan, record["run_id"]), [])
            resumed = subprocess.run([sys.executable, "-m", "reproforge", "run", str(plan.path),
                                      "--resume", record["run_id"]], capture_output=True, text=True, timeout=30)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            ledgers = [json.loads(path.read_text(encoding="utf-8")) for path in store.runs.glob("*.json")]
            result = next(item for item in ledgers if item["run_id"] != record["run_id"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["tasks"][0]["resumed_from"], record["run_id"])
            self.assertNotIn("resumed_from", result["tasks"][1])
            self.assertEqual(runner.verify(plan, result["run_id"]), [])
            self.assertEqual(ledger.read_bytes(), source_bytes)
            reference = result["tasks"][1]["outputs"]["two.txt"]
            self.assertEqual(store.object_path(reference["sha256"]).read_text(), "synthetic-done")


if __name__ == "__main__":
    unittest.main()
