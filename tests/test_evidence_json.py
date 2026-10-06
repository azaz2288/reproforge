"""Synthetic evidence parsing regressions, runnable against installed wheels."""

import contextlib
import copy
import http.client
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from reproforge.__main__ import main
from reproforge.dashboard import make_server, render
from reproforge.runner import execute, recover, verify
from reproforge.spec import SpecError, load_plan
from reproforge.storage import Store, StorageError


def duplicate_field(record, name, wrong):
    """Wrong first value, valid last value: a default decoder erases evidence."""
    encoded = json.dumps(record)
    return '{' + json.dumps(name) + ':' + json.dumps(wrong) + ',' + encoded[1:]


class EvidenceJSONTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'project.json'
        self.spec = {'version': 1, 'tasks': [{'id': 'first', 'command': [
            '@python', '-c', "from pathlib import Path; Path('out.txt').write_text('result')"],
            'outputs': ['out.txt'], 'cache': True}]}
        self.path.write_text(json.dumps(self.spec), encoding='utf-8')
        self.plan = load_plan(self.path)

    def run_fixture(self):
        record = execute(self.plan)
        self.assertEqual(record['status'], 'success')
        store = Store(self.root / '.reproforge')
        ledger = store.runs / f"{record['run_id']}.json"
        return record, store, ledger

    def test_spec_duplicate_keys_rejected_including_nested_and_escaped(self):
        invalid = [duplicate_field(self.spec, 'version', 2),
                   json.dumps(self.spec).replace('"cache": true', '"cache": false,"cache": true'),
                   json.dumps(self.spec).replace('"version": 1', '"version": 2,"versi\\u006fn": 1')]
        for content in invalid:
            self.path.write_text(content, encoding='utf-8')
            with self.subTest(content=content), self.assertRaises(SpecError):
                load_plan(self.path)

    def test_spec_nonfinite_unknown_task_metadata_rejected(self):
        for literal in ('NaN', 'Infinity', '-Infinity', '1e400'):
            self.path.write_text(json.dumps(self.spec).replace('"cache": true', f'"cache": true,"note":{literal}'), encoding='utf-8')
            with self.subTest(literal=literal), self.assertRaises(SpecError):
                load_plan(self.path)

    def test_verify_duplicate_or_nonfinite_ledger_rejected(self):
        record, store, ledger = self.run_fixture()
        invalid = [duplicate_field(record, 'status', 'failed'),
                   json.dumps(record).replace('"returncode": 0', '"returncode": 7,"returncode":0'),
                   json.dumps(record)[:-1] + ',"note":NaN}',
                   json.dumps(record)[:-1] + ',"note":1e400}']
        for content in invalid:
            ledger.write_text(content, encoding='utf-8')
            with self.subTest(content=content), self.assertRaises(StorageError):
                verify(self.plan, record['run_id'])

    def test_deep_and_invalid_utf8_evidence_is_controlled(self):
        record, store, ledger = self.run_fixture()
        for data in (b'[' * 5000 + b'0' + b']' * 5000, b'\xff'):
            ledger.write_bytes(data)
            with self.subTest(kind=data[:1]), self.assertRaises(StorageError):
                verify(self.plan, record['run_id'])
            self.assertEqual(recover(self.plan), [])
            self.assertEqual(ledger.read_bytes(), data)
            self.assertNotIn(record['run_id'], render(self.plan))
            self.path.write_bytes(data)
            with self.assertRaises(SpecError):
                load_plan(self.path)

    def test_valid_unicode_cache_and_resume_remain_compatible(self):
        self.spec['tasks'][0]['command'][2] = "from pathlib import Path; Path('out.txt').write_text('合成结果', encoding='utf-8')"
        self.path.write_bytes(b'\xef\xbb\xbf' + json.dumps(self.spec, ensure_ascii=False).encode('utf-8'))
        self.plan = load_plan(self.path)
        record, store, ledger = self.run_fixture()
        reused = execute(self.plan, reuse=True)
        self.assertEqual(reused['status'], 'success')
        self.assertEqual(reused['tasks'][0]['cached_from'], record['run_id'])
        self.assertEqual(verify(self.plan, reused['run_id']), [])
        record['status'] = 'interrupted'
        ledger.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        resumed = execute(self.plan, resume=record['run_id'])
        self.assertEqual(resumed['status'], 'success')
        self.assertEqual(verify(self.plan, resumed['run_id']), [])
        self.assertEqual(store.object_path(resumed['tasks'][0]['outputs']['out.txt']['sha256']).read_text(encoding='utf-8'), '合成结果')

    def test_numeric_versions_and_exit_codes_not_booleans(self):
        record, store, ledger = self.run_fixture()
        for field in ('version', 'returncode'):
            value = copy.deepcopy(record)
            if field == 'version':
                value[field] = True
            else:
                value['tasks'][0][field] = False
            ledger.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(field=field):
                try:
                    issues = verify(self.plan, record['run_id'])
                except StorageError:
                    continue
                self.assertTrue(issues)

    def test_verification_of_cached_run_reads_source_strictly(self):
        record, store, ledger = self.run_fixture()
        reused = execute(self.plan, reuse=True)
        self.assertEqual(verify(self.plan, reused['run_id']), [])
        ledger.write_text(duplicate_field(record, 'version', 2), encoding='utf-8')
        with self.assertRaises(StorageError):
            verify(self.plan, reused['run_id'])

    def test_verification_of_resumed_run_reads_source_strictly(self):
        record, store, ledger = self.run_fixture()
        record['status'] = 'interrupted'
        ledger.write_text(json.dumps(record), encoding='utf-8')
        resumed = execute(self.plan, resume=record['run_id'])
        self.assertEqual(verify(self.plan, resumed['run_id']), [])
        ledger.write_text(duplicate_field(record, 'status', 'running'), encoding='utf-8')
        with self.assertRaises(StorageError):
            verify(self.plan, resumed['run_id'])

    def test_cache_duplicate_index_and_source_fail_closed(self):
        record, store, ledger = self.run_fixture()
        cache = next(store.cache.glob('*.json'))
        original = cache.read_bytes()
        cache.write_text(duplicate_field(json.loads(original), 'version', 99), encoding='utf-8')
        reused = execute(self.plan, reuse=True)
        self.assertEqual(reused['status'], 'failed')
        self.assertNotIn('cached_from', reused['tasks'][0])
        cache.write_bytes(original)
        ledger.write_text(duplicate_field(record, 'status', 'failed'), encoding='utf-8')
        reused = execute(self.plan, reuse=True)
        self.assertEqual(reused['status'], 'failed')
        self.assertNotIn('cached_from', reused['tasks'][0])

    def test_recovery_does_not_rewrite_ambiguous_record(self):
        record, store, ledger = self.run_fixture()
        record['status'] = 'running'
        record.pop('finished_at')
        content = duplicate_field(record, 'status', 'failed').encode('utf-8')
        ledger.write_bytes(content)
        self.assertEqual(recover(self.plan), [])
        self.assertEqual(ledger.read_bytes(), content)

    def test_resume_rejects_ambiguous_source_without_new_run(self):
        record, store, ledger = self.run_fixture()
        record['status'] = 'interrupted'
        ledger.write_text(duplicate_field(record, 'status', 'running'), encoding='utf-8')
        before = sorted(store.runs.glob('*.json'))
        with self.assertRaises(StorageError):
            execute(self.plan, resume=record['run_id'])
        self.assertEqual(sorted(store.runs.glob('*.json')), before)

    def test_gates_reject_rehashed_duplicate_and_wrong_bool_type(self):
        (self.root / 'data.csv').write_text('id\n1\n', encoding='utf-8')
        self.path.write_text(json.dumps({'version': 1, 'tasks': [{'id': 'quality',
            'inputs': [{'project': 'data.csv', 'as': 'data.csv'}],
            'gate': {'type': 'csv_quality', 'input': 'data.csv'}}]}), encoding='utf-8')
        self.plan = load_plan(self.path)
        record, store, ledger = self.run_fixture()
        original = store.object_path(record['tasks'][0]['outputs']['gate-report.json']['sha256'])
        report = json.loads(original.read_text(encoding='utf-8'))
        for content in (duplicate_field(report, 'passed', False),
                        json.dumps({**report, 'passed': 1})):
            altered = self.root / 'altered.json'
            altered.write_text(content, encoding='utf-8')
            changed = copy.deepcopy(record)
            changed['tasks'][0]['outputs']['gate-report.json'] = store.put(altered)
            ledger.write_text(json.dumps(changed), encoding='utf-8')
            with self.subTest(content=content):
                try:
                    issues = verify(self.plan, record['run_id'])
                except StorageError:
                    continue
                self.assertTrue(issues)

    def test_cli_rejects_bad_plan_and_ledger_without_traceback(self):
        record, store, ledger = self.run_fixture()
        ledger.write_text(duplicate_field(record, 'status', 'failed'), encoding='utf-8')
        result = subprocess.run([sys.executable, '-m', 'reproforge', 'verify', str(self.path), record['run_id']],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.path.write_text(duplicate_field(self.spec, 'version', 2), encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['validate', str(self.path)]), 2)

    def test_dashboard_omits_ambiguous_run_real_http(self):
        record, store, ledger = self.run_fixture()
        ledger.write_text(duplicate_field(record, 'status', 'failed'), encoding='utf-8')
        self.assertNotIn(record['run_id'], render(self.plan))
        server = make_server(self.plan, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request('GET', '/')
            response = connection.getresponse()
            body = response.read().decode('utf-8')
            self.assertEqual(response.status, 200)
            self.assertNotIn(record['run_id'], body)
            self.assertIn('No runs yet', body)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
