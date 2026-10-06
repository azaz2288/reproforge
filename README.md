# ReproForge

ReproForge is a local-first, auditable execution engine for file-based research and data pipelines. It validates a declared DAG, materializes only declared inputs into each task's working directory, executes an argument vector without a shell, stores input/output bytes by SHA-256, and writes an atomic run ledger that can be checked later. It requires Python 3.12+ and has no runtime third-party dependencies.

The [project plan](PROJECT_PLAN.md) describes the larger platform, intended users, architecture, five milestones and honest limits. The current version contains the **M1 kernel and tested slices of M2–M4**, not the finished platform. In particular, the working directory is not a security sandbox: task commands run with the caller's system permissions. Run only trusted project definitions. A general plugin API, partial-task resume, multi-user access control and M5 scale validation are not implemented yet.

## Quick start

From this repository root:

```sh
python -m reproforge validate examples/hello_pipeline/project.json
python -m reproforge run examples/hello_pipeline/project.json
python -m reproforge verify examples/hello_pipeline/project.json RUN_ID_FROM_PREVIOUS_COMMAND
python -m unittest discover -s tests -v
python -m reproforge run examples/quality_pipeline/project.json
python -m reproforge serve examples/quality_pipeline/project.json --port 8765
```

Or install with `python -m pip install .` and use the `reproforge` console command. The example intentionally lists `report` before `summary`; validation orders it by the artifact dependency. Run state is stored inside the example project at `.reproforge/` (ignored by Git). Each run has a UUID-like 32-character ID, a JSON ledger and content-addressed input/output/log objects. `verify` checks the current spec hash, task order/contracts, dependency references and all referenced object bytes without rerunning commands. It reports drift from the current spec, while stored historical artifacts remain available. Exit codes: 0 success/valid, 1 task failure or verification violations, 2 invalid spec or storage error.

## Specification v1

Each task has an ID, `command` argument list, optional `inputs` and `depends_on`, declared `outputs`, and optional `timeout_seconds` (1–3600, default 300) and `cache` (default false). Use `@python` as the first command item to run the current Python interpreter. Inputs are either `{"project": "source/path", "as": "workspace/path"}` or `{"task": "upstream-id", "artifact": "output/path", "as": "workspace/path"}`. Task references automatically create dependencies. Paths use project/workspace-relative POSIX syntax and cannot traverse upward or use `.reproforge`. Source inputs and output objects are recorded by hash.

Task stdout/stderr are retained as full content-addressed objects, with a short preview in the ledger. A nonzero exit, timeout or missing declared output fails the run and prevents later tasks. Runs write their ledger atomically after each task. Existing run state is not deleted automatically.

## Built-in gates and recovery

A gate is a task with one declared CSV input, no `command`, no cache, and a fixed `gate-report.json` output. `csv_quality` checks required columns, minimum rows, maximum null fraction and optional duplicate keys. `time_split` checks ISO timestamps and requires every train timestamp to precede every test timestamp. Violations produce a stored JSON report and block downstream tasks. `verify` reevaluates the stored input and compares the report. The [three-step example](examples/quality_pipeline/project.json) demonstrates both gates feeding a report task; its CSV is synthetic, not a real-world dataset.

Only one writer can execute a project at a time. `reproforge recover project.json` marks abandoned `running` ledgers as `interrupted` after taking the project lock. It does not resume commands or undo side effects. `reproforge serve project.json` opens a read-only dashboard on `127.0.0.1` for runs, dependencies, artifact hashes and comparison. It is not authenticated; never expose it on a public network.

To retry a failed or interrupted run without recomputing its successful prefix, use `reproforge run project.json --resume RUN_ID`. This creates a **new** run ledger. The source ledger and stored objects must verify, the plan and runtime environment must match, and each resumed task's live declared inputs must still match. A changed input or corrupt source fails closed. This does not undo external side effects or guarantee determinism for commands that read undeclared state. `--reuse` is separate opt-in cache behavior.

The [benchmark and recovery notes](BENCHMARKS.md) show a repeatable synthetic-data test and an operational recovery exercise. They do not establish full M5 scale or reproducibility on real-world data.

The [UCI Iris case](examples/iris_case/README.md) is an opt-in real public-data pipeline with a pinned raw download, quality gate, simple baseline model and traceable report. CI uses an offline fixture; the actual public-data run was verified locally.

## Experimental opt-in reuse (M2 in progress)

Tasks may add `"cache": true` only if their result is deterministic from their declared inputs, command, whole plan and recorded runtime environment. Even then, reuse happens only with `reproforge run project.json --reuse`. The cache key includes the plan bytes, command/timeout, declared output names, content hashes of inputs, Python version, platform and interpreter path. Reused outputs and logs are checked against their SHA-256 objects and the source run ledger. Any corrupt index or object fails the run rather than silently reusing it; changing an input, plan or recorded environment produces a new key.

This is **not** a hermetic build cache. The engine cannot detect undeclared files, network responses, locale, environment variables, imported package changes or hidden randomness. Do not enable caching for tasks that depend on them. Resume reuses a verified successful prefix but does not restore a partially executing task. Broader environment capture is still open M2 work. Reuse is disabled by default.

## Limits

v0.4.3 rejects ambiguous JSON across project specifications, run ledgers, cache indexes and their source ledgers, resume chains, and stored gate reports. Duplicate keys (including escaped names and nested objects), NaN/Infinity literals, float overflow and decoder nesting failures are controlled input errors, not last-value-wins evidence. Version and task exit-code fields require integers, not JSON booleans. Gate comparison distinguishes booleans from numbers recursively, even if someone rehashes the altered report object. This is internal-consistency validation, not authentication or a signature.

`verify` and plan validation return exit 2 for malformed evidence; a well-formed but inconsistent gate/report returns exit 1. Cache corruption fails the new task instead of falling back silently; resume rejects an ambiguous source before creating a new ledger. `recover` skips malformed or unsupported records without rewriting their bytes, and the read-only dashboard omits these records. Dashboard visibility itself does not imply independent verification. Existing valid version-1 records remain supported; manually modified ambiguous records must be inspected externally or regenerated, not silently repaired. The decoder does not provide a file-size/memory quota or protection against all hostile local filesystem races.

Run a self-contained **synthetic** example, using only a generated temporary project:

```sh
python -m examples.evidence_audit
python -m examples.evidence_audit --serve --port 8891
python -m unittest discover -s tests -p test_evidence_json.py -v
```

The demo verifies two successful runs and cache reuse, rejects a duplicate-status ledger, proves recovery preserves its bytes and the dashboard omits it. With `--serve`, a loopback-only demo dashboard stays available until interrupted; it never inspects your existing projects. The CLI still uses trusted commands and does not kill descendant processes on timeout.

v0.4.2 flushes and fsyncs complete temporary JSON before atomically replacing a run/gate/cache checkpoint. Serialization, sync or replacement failure preserves the previous checkpoint (or no initial record); no successful final ledger means no new cache publication. File sync is **not** a directory-fsync or power-loss guarantee. Inspect an incomplete run, then use `recover` and a new `run --resume` after independently verifying objects. External command side effects are not rolled back.

Recovery fault tests now include an actual executor subprocess exiting with `os._exit` after its first successful checkpoint, OS writer-lock release, CLI recovery, and a new CLI run reusing only the verified prefix. They also test final-ledger publication failure after all tasks finish: no new cache, explicit recovery, both successful tasks reused, source ledger unchanged. These are generated temporary workflows, not hand-edited running-status fixtures, but do not certify mid-command kill/descendant termination, disk-full hardware, filesystem crash, or electrical power loss.

```sh
python -m unittest discover -s tests -p test_recovery_faults.py -v
```

v0.4.1 verifies a temporary object against the expected digest and size **before** publishing it, then uses an exclusive hard link instead of replacing an immutable object. Concurrent duplicate publication validates the existing object without overwriting or deleting it. Source mutation and I/O failure cannot expose invalid bytes under a trusted digest. This requires local filesystem hard-link support; failure is explicit, with no unsafe replacement fallback. File fsync is not a guarantee of directory-entry durability after power loss, and the store still assumes trusted local writers.

- A task can still read other files, use the network or spawn child processes; the temporary directory is organizational isolation, not a security boundary. A timeout kills the direct process, not necessarily its descendants.
- Experimental M2 caching is opt-in; writers are serialized per project, but dependency versions are not pinned and arbitrary commands are not guaranteed deterministic.
- Source bytes are copied into the object store before a task runs. This records the bytes used, but does not establish authenticity of external data or prevent a malicious command from affecting the host.
- Verification checks internal consistency and stored bytes, not a signed attestation. A party able to modify the ledger and all objects can forge a consistent history.
- Large files are copied; streaming/zero-copy optimizations are planned only after measurement.

This repository is newly created for portfolio work and cannot qualify as a pre-existing repository under the linked Feishu collection rules.
