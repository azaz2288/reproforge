"""Temporary synthetic evidence demo; does not inspect any existing project."""

import argparse
import json
import tempfile
from pathlib import Path

from reproforge.dashboard import make_server, render
from reproforge.runner import execute, recover, verify
from reproforge.spec import load_plan
from reproforge.storage import StorageError, Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true', help='show generated dashboard until interrupted')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('port must be 0..65535')
    with tempfile.TemporaryDirectory(prefix='reproforge-evidence-demo-') as temporary:
        root = Path(temporary)
        path = root / 'project.json'
        path.write_text(json.dumps({'version': 1, 'tasks': [{'id': 'synthetic',
            'command': ['@python', '-c', "from pathlib import Path; Path('out.txt').write_text('synthetic only')"],
            'outputs': ['out.txt'], 'cache': True}]}), encoding='utf-8')
        plan = load_plan(path)
        run = execute(plan)
        assert run['status'] == 'success' and verify(plan, run['run_id']) == []
        reused = execute(plan, reuse=True)
        assert reused['status'] == 'success' and verify(plan, reused['run_id']) == []
        assert reused['tasks'][0]['cached_from'] == run['run_id']
        store = Store(root / '.reproforge')
        invalid_id = 'a' * 32
        invalid = {**run, 'run_id': invalid_id, 'status': 'running'}
        invalid.pop('finished_at')
        content = '{"status":"failed",' + json.dumps(invalid)[1:]
        bad_path = store.runs / f'{invalid_id}.json'
        bad_path.write_text(content, encoding='utf-8')
        try:
            verify(plan, invalid_id)
        except StorageError:
            pass
        else:
            raise AssertionError('ambiguous ledger accepted')
        assert recover(plan) == [] and bad_path.read_text(encoding='utf-8') == content
        assert invalid_id not in render(plan)
        print(json.dumps({'synthetic': True, 'verified_runs': 2, 'cache_reused': True,
                          'ambiguous_ledger_rejected': True, 'recovery_preserved_bytes': True,
                          'dashboard_omitted_invalid_run': True}), flush=True)
        if args.serve:
            with make_server(plan, args.port) as server:
                print(f'Synthetic dashboard: http://127.0.0.1:{server.server_port}/', flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass


if __name__ == '__main__':
    main()
