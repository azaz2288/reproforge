# Benchmark and recovery notes

## Reproducible local measurement

Run `python -m benchmarks.quality_gate --rows 1000000` from the repository root. This creates a temporary synthetic CSV, runs a built-in quality gate, independently verifies the result and prints JSON timing and peak Python allocation. The script does not report total process RSS or disk usage, and results depend on hardware, filesystem and Python version.

One Windows/Python 3.12-compatible local run on 2026-09-30 yielded:

| Rows | CSV bytes | Run seconds | Verify seconds | Peak traced Python bytes |
|---:|---:|---:|---:|---:|
| 10,000 | 77,899 | 0.351 | 0.181 | 1,389,868 |
| 100,000 | 878,899 | 1.661 | 1.528 | 2,191,119 |
| 1,000,000 | 9,788,899 | 14.289 | 14.924 | 2,360,700 |

This demonstrates a bounded-memory CSV gate on a synthetic million-row input, **not** production-scale validation. The optional `unique_by` rule retains keys in memory and is not bounded-memory. Large-object copying, multi-gigabyte datasets, remote storage and real public-data case studies remain open M5 work.

## Recovery exercise

If a process stops mid-run, its last durable ledger remains `running`. After confirming no earlier ReproForge process is active, run `reproforge recover PROJECT_JSON`; the command takes the project writer lock and marks matching abandoned ledgers `interrupted`. It never resumes a command or deletes objects. Inspect the run with `reproforge verify PROJECT_JSON RUN_ID`; then start a new run. Task side effects outside the staging directory are not rolled back. If the CLI reports a lock timeout, wait for the active process or investigate it; do not delete the lock file while it might be held.
