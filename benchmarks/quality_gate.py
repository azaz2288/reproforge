"""Repeatable local gate benchmark; reports observed time and memory, not a claim of scale."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from reproforge.runner import execute, verify
from reproforge.spec import load_plan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100_000)
    args = parser.parse_args()
    if not 1 <= args.rows <= 1_000_000:
        parser.error("--rows must be between 1 and 1,000,000")
    with tempfile.TemporaryDirectory(prefix="reproforge-benchmark-") as temporary:
        root = Path(temporary)
        with (root / "data.csv").open("w", encoding="utf-8", newline="") as stream:
            stream.write("id,value\n")
            for number in range(args.rows):
                stream.write(f"{number},{number % 100}\n")
        specification = {"version": 1, "tasks": [{"id": "quality",
            "inputs": [{"project": "data.csv", "as": "data.csv"}],
            "gate": {"type": "csv_quality", "input": "data.csv", "required_columns": ["id", "value"],
                     "min_rows": args.rows, "max_null_fraction": 0}}]}
        path = root / "project.json"
        path.write_text(json.dumps(specification), encoding="utf-8")
        plan = load_plan(path)
        tracemalloc.start()
        start = time.perf_counter()
        record = execute(plan)
        run_seconds = time.perf_counter() - start
        start = time.perf_counter()
        issues = verify(plan, record["run_id"])
        verify_seconds = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(json.dumps({"rows": args.rows, "csv_bytes": (root / "data.csv").stat().st_size,
                          "run_seconds": round(run_seconds, 3), "verify_seconds": round(verify_seconds, 3),
                          "peak_python_bytes": peak, "run_status": record["status"], "verify_issues": issues}, indent=2))
        if record["status"] != "success" or issues:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
