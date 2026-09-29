import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reproforge.runner import execute, verify
from reproforge.spec import SpecError, load_plan
from reproforge.storage import Store


def project(root: Path, tasks: list[dict]) -> Path:
    path = root / "project.json"
    path.write_text(json.dumps({"version": 1, "tasks": tasks}), encoding="utf-8")
    return path


def task(task_id: str, script: str, inputs: list[dict] | None = None,
         outputs: list[str] | None = None, timeout: int = 5) -> dict:
    return {"id": task_id, "command": ["@python", "-c", script], "inputs": inputs or [],
            "outputs": outputs if outputs is not None else ["result.txt"], "timeout_seconds": timeout}


class SpecificationTests(unittest.TestCase):
    def test_topological_order_and_artifact_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            later = task("later", "from pathlib import Path; Path('b.txt').write_text(Path('a.txt').read_text())",
                         [{"task": "first", "artifact": "a.txt", "as": "a.txt"}], ["b.txt"])
            first = task("first", "from pathlib import Path; Path('a.txt').write_text('ok')", outputs=["a.txt"])
            plan = load_plan(project(root, [later, first]))
            self.assertEqual([item.id for item in plan.tasks], ["first", "later"])

    def test_rejects_cycle_traversal_and_overlaps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = task("a", "pass", [{"task": "b", "artifact": "x", "as": "x"}], ["y"])
            b = task("b", "pass", [{"task": "a", "artifact": "y", "as": "y"}], ["x"])
            with self.assertRaisesRegex(SpecError, "cycle"):
                load_plan(project(root, [a, b]))
            bad = task("bad", "pass", [{"project": "../secret", "as": "input"}])
            with self.assertRaisesRegex(SpecError, "forbidden"):
                load_plan(project(root, [bad]))
            for unsafe in ("CON.txt", "folder/name:stream", "folder/trailing."):
                with self.subTest(unsafe=unsafe), self.assertRaisesRegex(SpecError, "forbidden"):
                    load_plan(project(root, [task("bad", "pass", outputs=[unsafe])]))
            bad = task("bad", "pass", outputs=["a", "a-b", "a/b"])
            with self.assertRaisesRegex(SpecError, "overlap"):
                load_plan(project(root, [bad]))
            bad = task("bad", "pass", [{"project": "x", "as": "a"}, {"project": "y", "as": "a/b"}])
            with self.assertRaisesRegex(SpecError, "overlap"):
                load_plan(project(root, [bad]))

    def test_rejects_unknown_artifact_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = task("first", "pass", outputs=["x"])
            bad = task("bad", "pass", [{"task": "first", "artifact": "missing", "as": "x"}])
            with self.assertRaisesRegex(SpecError, "does not declare"):
                load_plan(project(root, [first, bad]))
            with self.assertRaisesRegex(SpecError, "Duplicate task IDs"):
                load_plan(project(root, [first, copy.deepcopy(first)]))


class ExecutionTests(unittest.TestCase):
    def test_multistep_run_and_independent_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.txt").write_text("hello", encoding="utf-8")
            first = task("first", "from pathlib import Path; Path('one.txt').write_text(Path('source.txt').read_text().upper())",
                         [{"project": "source.txt", "as": "source.txt"}], ["one.txt"])
            second = task("second", "from pathlib import Path; Path('two.txt').write_text(Path('one.txt').read_text()+'!')",
                          [{"task": "first", "artifact": "one.txt", "as": "one.txt"}], ["two.txt"])
            plan = load_plan(project(root, [second, first]))
            record = execute(plan)
            self.assertEqual(record["status"], "success")
            self.assertEqual([entry["id"] for entry in record["tasks"]], ["first", "second"])
            self.assertEqual(verify(plan, record["run_id"]), [])
            reference = record["tasks"][1]["outputs"]["two.txt"]
            self.assertEqual(Store(root / ".reproforge").object_path(reference["sha256"]).read_text(), "HELLO!")
            (root / "source.txt").write_text("changed", encoding="utf-8")
            self.assertEqual(verify(plan, record["run_id"]), [], "Historical bytes are preserved in the object store")

    def test_detects_tampered_object_and_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_plan(project(root, [task("one", "from pathlib import Path; Path('result.txt').write_text('ok')")]))
            record = execute(plan)
            store = Store(root / ".reproforge")
            output = record["tasks"][0]["outputs"]["result.txt"]
            store.object_path(output["sha256"]).write_text("tampered", encoding="utf-8")
            self.assertTrue(any("Corrupt object" in issue for issue in verify(plan, record["run_id"])))
            ledger = store.runs / f"{record['run_id']}.json"
            saved = json.loads(ledger.read_text(encoding="utf-8"))
            saved["tasks"][0]["command"] = ["different"]
            ledger.write_text(json.dumps(saved), encoding="utf-8")
            self.assertTrue(any("does not match" in issue for issue in verify(plan, record["run_id"])))

    def test_failure_timeout_and_missing_output_leave_records(self):
        scenarios = [
            ("import sys; sys.exit(7)", "failed"),
            ("import time; time.sleep(2)", "timeout"),
            ("print('no output')", "failed"),
        ]
        for script, expected in scenarios:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan = load_plan(project(root, [task("one", script, timeout=1)]))
                record = execute(plan)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["tasks"][0]["status"], expected)
                self.assertEqual(verify(plan, record["run_id"]), [])

    def test_incomplete_record_is_not_reported_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_plan(project(root, [task("one", "pass")]))
            run_id = "a" * 32
            store = Store(root / ".reproforge")
            store.runs.mkdir(parents=True)
            (store.runs / f"{run_id}.json").write_text(json.dumps({"version": 1, "run_id": run_id,
                "status": "running", "plan_sha256": __import__("hashlib").sha256(plan.raw_bytes).hexdigest(), "tasks": []}), encoding="utf-8")
            self.assertTrue(any("incomplete" in issue for issue in verify(plan, run_id)))

    def test_detects_log_preview_and_exit_status_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_plan(project(root, [task("one", "from pathlib import Path; print('logged'); Path('result.txt').write_text('ok')")]))
            record = execute(plan)
            ledger = Store(root / ".reproforge").runs / f"{record['run_id']}.json"
            saved = json.loads(ledger.read_text(encoding="utf-8"))
            saved["tasks"][0]["stdout"]["preview"] = "fabricated"
            saved["tasks"][0]["returncode"] = 7
            ledger.write_text(json.dumps(saved), encoding="utf-8")
            issues = verify(plan, record["run_id"])
            self.assertTrue(any("preview metadata mismatch" in issue for issue in issues))
            self.assertTrue(any("nonzero or missing exit code" in issue for issue in issues))

    def test_cli_exit_codes_and_plan_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = project(root, [task("one", "from pathlib import Path; Path('result.txt').write_text('ok')")])
            run = subprocess.run([sys.executable, "-m", "reproforge", "run", str(path)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            run_id = run.stdout.split("Run ", 1)[1].split(":", 1)[0]
            verify_result = subprocess.run([sys.executable, "-m", "reproforge", "verify", str(path), run_id], capture_output=True, text=True)
            self.assertEqual(verify_result.returncode, 0, verify_result.stderr)
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            drift = subprocess.run([sys.executable, "-m", "reproforge", "verify", str(path), run_id], capture_output=True, text=True)
            self.assertEqual(drift.returncode, 1)
            self.assertIn("differs", drift.stdout)


if __name__ == "__main__":
    unittest.main()
