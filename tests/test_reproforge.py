import copy
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reproforge.locking import project_lock
from reproforge.dashboard import make_server, render
from reproforge.runner import _cache_key, execute, recover, verify
from reproforge.spec import SpecError, load_plan
from reproforge.storage import Store, StorageError


def project(root: Path, tasks: list[dict]) -> Path:
    path = root / "project.json"
    path.write_text(json.dumps({"version": 1, "tasks": tasks}), encoding="utf-8")
    return path


def task(task_id: str, script: str, inputs: list[dict] | None = None,
         outputs: list[str] | None = None, timeout: int = 5) -> dict:
    return {"id": task_id, "command": ["@python", "-c", script], "inputs": inputs or [],
            "outputs": outputs if outputs is not None else ["result.txt"], "timeout_seconds": timeout}


class SpecificationTests(unittest.TestCase):
    def test_rejects_invalid_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = {"id": "quality", "inputs": [{"project": "data.csv", "as": "data.csv"}],
                        "gate": {"type": "csv_quality", "input": "missing.csv"}}
            with self.assertRaisesRegex(SpecError, "declared input"):
                load_plan(project(root, [selected]))
            selected["gate"]["input"] = "data.csv"
            selected["gate"]["max_null_fraction"] = 2
            with self.assertRaisesRegex(SpecError, "thresholds"):
                load_plan(project(root, [selected]))

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
            first["cache"] = "yes"
            with self.assertRaisesRegex(SpecError, "cache must be"):
                load_plan(project(root, [first]))


class ExecutionTests(unittest.TestCase):
    def test_resume_reuses_verified_prefix_then_retries_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.txt").write_text("ok", encoding="utf-8")
            first = task("first", "from pathlib import Path; Path('one.txt').write_text(Path('source.txt').read_text())",
                         [{"project": "source.txt", "as": "source.txt"}], ["one.txt"])
            second = task("second", "from pathlib import Path; Path('two.txt').write_text(Path('one.txt').read_text())",
                          [{"task": "first", "artifact": "one.txt", "as": "one.txt"}], ["two.txt"])
            plan = load_plan(project(root, [first, second]))
            original = execute(plan)
            self.assertEqual(original["status"], "success")
            ledger = Store(root / ".reproforge").runs / f"{original['run_id']}.json"
            changed = json.loads(ledger.read_text(encoding="utf-8"))
            changed["status"] = "interrupted"
            changed["tasks"] = changed["tasks"][:1]
            ledger.write_text(json.dumps(changed), encoding="utf-8")
            self.assertEqual(verify(plan, original["run_id"]), [])
            resumed = execute(plan, resume=original["run_id"])
            self.assertEqual(resumed["status"], "success")
            self.assertEqual(resumed["tasks"][0]["resumed_from"], original["run_id"])
            self.assertNotIn("resumed_from", resumed["tasks"][1])
            self.assertEqual(verify(plan, resumed["run_id"]), [])

    def test_resume_rejects_changed_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("old", encoding="utf-8")
            first = task("first", "from pathlib import Path; Path('one.txt').write_text(Path('source.txt').read_text())",
                         [{"project": "source.txt", "as": "source.txt"}], ["one.txt"])
            second = task("second", "import sys; sys.exit(7)", outputs=[])
            plan = load_plan(project(root, [first, second]))
            failed = execute(plan)
            self.assertEqual(failed["status"], "failed")
            source.write_text("new", encoding="utf-8")
            resumed = execute(plan, resume=failed["run_id"])
            self.assertEqual(resumed["status"], "failed")
            self.assertIn("declared inputs changed", resumed["tasks"][0]["error"])
            self.assertEqual(verify(plan, resumed["run_id"]), [])

    def test_resume_rejects_corrupt_source_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = task("first", "from pathlib import Path; Path('one.txt').write_text('ok')", outputs=["one.txt"])
            second = task("second", "import sys; sys.exit(7)", outputs=[])
            plan = load_plan(project(root, [first, second]))
            failed = execute(plan)
            store = Store(root / ".reproforge")
            reference = failed["tasks"][0]["outputs"]["one.txt"]
            store.object_path(reference["sha256"]).write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(StorageError, "Cannot resume run"):
                execute(plan, resume=failed["run_id"])

    def test_iris_case_pipeline_with_offline_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            example = Path(__file__).resolve().parents[1] / "examples" / "iris_case"
            (root / "scripts").mkdir()
            (root / "data").mkdir()
            for name in ("train.py", "report.py"):
                shutil.copy2(example / "scripts" / name, root / "scripts" / name)
            shutil.copy2(example / "project.json", root / "project.json")
            lines = ["sepal_length,sepal_width,petal_length,petal_width,species"]
            for label, center in (("Iris-setosa", 1), ("Iris-versicolor", 5), ("Iris-virginica", 9)):
                lines.extend(f"{center}.{number % 5},2.0,3.0,4.0,{label}" for number in range(50))
            (root / "data" / "iris.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
            plan = load_plan(root / "project.json")
            run = execute(plan)
            self.assertEqual(run["status"], "success")
            self.assertEqual(verify(plan, run["run_id"]), [])

    def test_recover_marks_only_abandoned_matching_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_plan(project(root, [task("one", "pass")]))
            run_id = "a" * 32
            store = Store(root / ".reproforge")
            store.runs.mkdir(parents=True)
            ledger = store.runs / f"{run_id}.json"
            ledger.write_text(json.dumps({"version": 1, "run_id": run_id, "status": "running",
                "plan_sha256": __import__("hashlib").sha256(plan.raw_bytes).hexdigest(),
                "environment": {}, "tasks": []}), encoding="utf-8")
            self.assertEqual(recover(plan), [run_id])
            self.assertEqual(recover(plan), [])
            self.assertEqual(verify(plan, run_id), [])

    def test_dashboard_is_read_only_and_escapes_ledger_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = load_plan(project(root, [task("one", "from pathlib import Path; Path('result.txt').write_text('ok')")]))
            run = execute(plan)
            ledger = Store(root / ".reproforge").runs / f"{run['run_id']}.json"
            changed = json.loads(ledger.read_text(encoding="utf-8"))
            changed["tasks"][0]["error"] = "<script>alert(1)</script>"
            ledger.write_text(json.dumps(changed), encoding="utf-8")
            page = render(plan, run["run_id"])
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("<script>", page)
            server = make_server(plan, 0)
            import threading
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("Content-Security-Policy", response.headers)
                response.read()
                connection.request("POST", "/")
                self.assertEqual(connection.getresponse().status, 405)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_project_lock_serializes_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = ("import sys,time; from pathlib import Path; "
                      "from reproforge.locking import project_lock; "
                      "lock=project_lock(Path(sys.argv[1])); "
                      "lock.__enter__(); print('locked',flush=True); time.sleep(1); lock.__exit__(None,None,None)")
            holder = subprocess.Popen([sys.executable, "-c", script, str(root / ".reproforge")],
                                      stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(StorageError, "Timed out waiting"):
                    with project_lock(root / ".reproforge", timeout=0.1):
                        pass
            finally:
                holder.wait(timeout=5)
                holder.stdout.close()
            with project_lock(root / ".reproforge", timeout=0.1):
                pass

    def test_csv_gate_blocks_downstream_and_verifies_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data.csv").write_text("id,value\n1,ok\n1,\n", encoding="utf-8")
            gate = {"id": "quality", "inputs": [{"project": "data.csv", "as": "data.csv"}],
                    "gate": {"type": "csv_quality", "input": "data.csv", "required_columns": ["id", "value"],
                             "min_rows": 2, "max_null_fraction": 0.0, "unique_by": ["id"]}}
            downstream = task("publish", "from pathlib import Path; Path('result.txt').write_text('published')",
                              [{"task": "quality", "artifact": "gate-report.json", "as": "gate-report.json"}])
            plan = load_plan(project(root, [downstream, gate]))
            result = execute(plan)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(result["tasks"]), 1)
            self.assertIn("duplicate unique keys", result["tasks"][0]["error"])
            self.assertEqual(verify(plan, result["run_id"]), [])
            report_ref = result["tasks"][0]["outputs"]["gate-report.json"]
            report_path = Store(root / ".reproforge").object_path(report_ref["sha256"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["passed"])
            report["passed"] = True
            forged = root / "forged.json"
            forged.write_text(json.dumps(report), encoding="utf-8")
            forged_ref = Store(root / ".reproforge").put(forged)
            ledger = Store(root / ".reproforge").runs / f"{result['run_id']}.json"
            changed = json.loads(ledger.read_text(encoding="utf-8"))
            changed["tasks"][0]["outputs"]["gate-report.json"] = forged_ref
            ledger.write_text(json.dumps(changed), encoding="utf-8")
            self.assertTrue(any("gate report differs" in issue for issue in verify(plan, result["run_id"])))

    def test_temporal_gate_catches_leakage_and_passes_ordered_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "splits.csv"
            selected = {"id": "timecheck", "inputs": [{"project": "splits.csv", "as": "splits.csv"}],
                        "gate": {"type": "time_split", "input": "splits.csv", "timestamp_column": "date",
                                 "split_column": "split", "train_label": "train", "test_label": "test"}}
            plan = load_plan(project(root, [selected]))
            source.write_text("date,split\n2025-02-01,train\n2025-01-01,test\n", encoding="utf-8")
            failed = execute(plan)
            self.assertEqual(failed["status"], "failed")
            self.assertIn("Temporal leakage", failed["tasks"][0]["error"])
            self.assertEqual(verify(plan, failed["run_id"]), [])
            source.write_text("date,split\n2025-01-01,train\n2025-02-01,test\n", encoding="utf-8")
            passed = execute(plan)
            self.assertEqual(passed["status"], "success")
            self.assertEqual(verify(plan, passed["run_id"]), [])

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
                "status": "running", "plan_sha256": __import__("hashlib").sha256(plan.raw_bytes).hexdigest(),
                "environment": {}, "tasks": []}), encoding="utf-8")
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

    def test_cache_is_opt_in_and_input_change_invalidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.txt").write_text("first", encoding="utf-8")
            script = ("from pathlib import Path; "
                      "Path('result.txt').write_text(Path('source.txt').read_text().upper())")
            selected = task("one", script, [{"project": "source.txt", "as": "source.txt"}])
            selected["cache"] = True
            plan = load_plan(project(root, [selected]))
            first = execute(plan)
            self.assertNotIn("cached_from", first["tasks"][0])
            second = execute(plan)
            self.assertNotIn("cached_from", second["tasks"][0], "Reuse is off unless explicitly requested")
            third = execute(plan, reuse=True)
            self.assertEqual(third["tasks"][0]["cached_from"], second["run_id"])
            self.assertEqual(verify(plan, third["run_id"]), [])
            (root / "source.txt").write_text("changed", encoding="utf-8")
            fourth = execute(plan, reuse=True)
            self.assertNotIn("cached_from", fourth["tasks"][0])
            self.assertNotEqual(fourth["tasks"][0]["outputs"], third["tasks"][0]["outputs"])

    def test_cache_rejects_corrupt_object_and_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = task("one", "from pathlib import Path; Path('result.txt').write_text('ok')")
            selected["cache"] = True
            plan = load_plan(project(root, [selected]))
            first = execute(plan)
            store = Store(root / ".reproforge")
            key = first["tasks"][0]["cache_key"]
            index = store.cache / f"{key}.json"
            cached = json.loads(index.read_text(encoding="utf-8"))
            cached["source_run_id"] = "a" * 32
            index.write_text(json.dumps(cached), encoding="utf-8")
            failed = execute(plan, reuse=True)
            self.assertEqual(failed["status"], "failed")
            self.assertIn("Cannot read cache source run", failed["tasks"][0]["error"])
            cached["source_run_id"] = first["run_id"]
            index.write_text(json.dumps(cached), encoding="utf-8")
            reference = first["tasks"][0]["outputs"]["result.txt"]
            store.object_path(reference["sha256"]).write_text("bad", encoding="utf-8")
            failed = execute(plan, reuse=True)
            self.assertEqual(failed["status"], "failed")
            self.assertIn("Cache object failed validation", failed["tasks"][0]["error"])

    def test_cache_rejects_incomplete_or_modified_source_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = task("one", "from pathlib import Path; Path('result.txt').write_text('ok')")
            selected["cache"] = True
            plan = load_plan(project(root, [selected]))
            first = execute(plan)
            ledger = Store(root / ".reproforge").runs / f"{first['run_id']}.json"
            original = json.loads(ledger.read_text(encoding="utf-8"))
            for field, value in (("status", "running"), ("environment", {})):
                with self.subTest(field=field):
                    changed = copy.deepcopy(original)
                    changed[field] = value
                    ledger.write_text(json.dumps(changed), encoding="utf-8")
                    failed = execute(plan, reuse=True)
                    self.assertEqual(failed["status"], "failed")
                    self.assertIn("Malformed cache source run", failed["tasks"][0]["error"])
            changed = copy.deepcopy(original)
            changed["tasks"][0]["returncode"] = 4
            ledger.write_text(json.dumps(changed), encoding="utf-8")
            failed = execute(plan, reuse=True)
            self.assertEqual(failed["status"], "failed")
            self.assertIn("Cache index does not match source run", failed["tasks"][0]["error"])

    def test_verify_rejects_modified_cache_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = task("one", "from pathlib import Path; Path('result.txt').write_text('ok')")
            selected["cache"] = True
            plan = load_plan(project(root, [selected]))
            first = execute(plan)
            second = execute(plan, reuse=True)
            self.assertEqual(verify(plan, second["run_id"]), [])
            ledger = Store(root / ".reproforge").runs / f"{first['run_id']}.json"
            changed = json.loads(ledger.read_text(encoding="utf-8"))
            changed["status"] = "failed"
            ledger.write_text(json.dumps(changed), encoding="utf-8")
            self.assertTrue(any("cached artifacts differ" in issue for issue in verify(plan, second["run_id"])))

    def test_cache_key_changes_with_plan_and_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = task("one", "pass")
            selected["cache"] = True
            path = project(root, [selected])
            plan = load_plan(path)
            key = _cache_key(plan, plan.tasks[0], [], {"python": "3.12", "platform": "A"})
            self.assertNotEqual(key, _cache_key(plan, plan.tasks[0], [], {"python": "3.12", "platform": "B"}))
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            changed = load_plan(path)
            self.assertNotEqual(key, _cache_key(changed, changed.tasks[0], [], {"python": "3.12", "platform": "A"}))

    def test_cached_dependency_feeds_downstream_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.txt").write_text("hello", encoding="utf-8")
            first = task("first", "from pathlib import Path; Path('upper.txt').write_text(Path('source.txt').read_text().upper())",
                         [{"project": "source.txt", "as": "source.txt"}], ["upper.txt"])
            first["cache"] = True
            second = task("second", "from pathlib import Path; Path('result.txt').write_text(Path('upper.txt').read_text()+'!')",
                          [{"task": "first", "artifact": "upper.txt", "as": "upper.txt"}])
            plan = load_plan(project(root, [second, first]))
            first_run = execute(plan)
            next_run = execute(plan, reuse=True)
            self.assertEqual(next_run["tasks"][0]["cached_from"], first_run["run_id"])
            self.assertEqual(next_run["tasks"][1]["status"], "success")
            self.assertNotIn("cached_from", next_run["tasks"][1])
            self.assertEqual(verify(plan, next_run["run_id"]), [])

    def test_cli_reuse_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = task("one", "from pathlib import Path; Path('result.txt').write_text('ok')")
            selected["cache"] = True
            path = project(root, [selected])
            command = [sys.executable, "-m", "reproforge", "run", str(path)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = subprocess.run([*command, "--reuse"], capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("reused verified artifacts", second.stdout)


if __name__ == "__main__":
    unittest.main()
