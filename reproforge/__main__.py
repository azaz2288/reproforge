"""ReproForge command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from .dashboard import make_server
from .runner import execute, recover, verify
from .spec import SpecError, load_plan
from .storage import StorageError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and verify local-first file pipelines")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run", "verify", "recover", "serve"):
        command = commands.add_parser(name)
        command.add_argument("project", type=Path, help="path to a version-1 project JSON specification")
        if name == "run":
            command.add_argument("--reuse", action="store_true", help="reuse verified outputs of tasks that explicitly declare cache=true")
            command.add_argument("--resume", metavar="RUN_ID", help="reuse the verified successful prefix of a failed or interrupted run")
        if name == "verify":
            command.add_argument("run_id")
        if name == "serve":
            command.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.project)
        if args.command == "validate":
            print(f"Plan SHA-256: {hashlib.sha256(plan.raw_bytes).hexdigest()}")
            for number, task in enumerate(plan.tasks, start=1):
                print(f"{number}. {task.id}: {len(task.inputs)} inputs, {len(task.outputs)} outputs")
            return 0
        if args.command == "run":
            record = execute(plan, reuse=args.reuse, resume=args.resume)
            print(f"Run {record['run_id']}: {record['status']}")
            for task in record["tasks"]:
                print(f"  {task['id']}: {task['status']}")
                if task.get("cached_from"):
                    print(f"    reused verified artifacts from {task['cached_from']}")
                if task.get("resumed_from"):
                    print(f"    resumed verified artifacts from {task['resumed_from']}")
                if task.get("error"):
                    print(f"    {task['error']}")
            return 0 if record["status"] == "success" else 1
        if args.command == "serve":
            if not 0 <= args.port <= 65535:
                parser.error("--port must be from 0 to 65535")
            with make_server(plan, args.port) as server:
                print(f"Read-only dashboard: http://127.0.0.1:{server.server_port}/", flush=True)
                server.serve_forever()
            return 0
        if args.command == "recover":
            recovered = recover(plan)
            print(f"Marked {len(recovered)} abandoned run(s) interrupted")
            for run_id in recovered:
                print(f"  {run_id}")
            return 0
        issues = verify(plan, args.run_id)
        if issues:
            for issue in issues:
                print(issue)
            return 1
        print(f"OK: run {args.run_id} matches its plan and stored objects")
        return 0
    except (SpecError, StorageError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
