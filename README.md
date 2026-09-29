# ReproForge

ReproForge is a local-first, auditable execution engine for file-based research and data pipelines. It validates a declared DAG, materializes only declared inputs into each task's working directory, executes an argument vector without a shell, stores input/output bytes by SHA-256, and writes an atomic run ledger that can be checked later. It requires Python 3.12+ and has no runtime third-party dependencies.

The [project plan](PROJECT_PLAN.md) describes the larger platform, intended users, architecture, five milestones and honest limits. The current version contains the **M1 kernel and an experimental M2 cache slice**, not the finished platform. In particular, the working directory is not a security sandbox: task commands run with the caller's system permissions. Run only trusted project definitions. Quality-gate plugins, concurrency coordination and a UI are not implemented yet.

## Quick start

From this repository root:

```sh
python -m reproforge validate examples/hello_pipeline/project.json
python -m reproforge run examples/hello_pipeline/project.json
python -m reproforge verify examples/hello_pipeline/project.json RUN_ID_FROM_PREVIOUS_COMMAND
python -m unittest discover -s tests -v
```

Or install with `python -m pip install .` and use the `reproforge` console command. The example intentionally lists `report` before `summary`; validation orders it by the artifact dependency. Run state is stored inside the example project at `.reproforge/` (ignored by Git). Each run has a UUID-like 32-character ID, a JSON ledger and content-addressed input/output/log objects. `verify` checks the current spec hash, task order/contracts, dependency references and all referenced object bytes without rerunning commands. It reports drift from the current spec, while stored historical artifacts remain available. Exit codes: 0 success/valid, 1 task failure or verification violations, 2 invalid spec or storage error.

## Specification v1

Each task has an ID, `command` argument list, optional `inputs` and `depends_on`, declared `outputs`, and optional `timeout_seconds` (1–3600, default 300) and `cache` (default false). Use `@python` as the first command item to run the current Python interpreter. Inputs are either `{"project": "source/path", "as": "workspace/path"}` or `{"task": "upstream-id", "artifact": "output/path", "as": "workspace/path"}`. Task references automatically create dependencies. Paths use project/workspace-relative POSIX syntax and cannot traverse upward or use `.reproforge`. Source inputs and output objects are recorded by hash.

Task stdout/stderr are retained as full content-addressed objects, with a short preview in the ledger. A nonzero exit, timeout or missing declared output fails the run and prevents later tasks. Runs write their ledger atomically after each task. Existing run state is not deleted automatically.

## Experimental opt-in reuse (M2 in progress)

Tasks may add `"cache": true` only if their result is deterministic from their declared inputs, command, whole plan and recorded runtime environment. Even then, reuse happens only with `reproforge run project.json --reuse`. The cache key includes the plan bytes, command/timeout, declared output names, content hashes of inputs, Python version, platform and interpreter path. Reused outputs and logs are checked against their SHA-256 objects and the source run ledger. Any corrupt index or object fails the run rather than silently reusing it; changing an input, plan or recorded environment produces a new key.

This is **not** a hermetic build cache. The engine cannot detect undeclared files, network responses, locale, environment variables, imported package changes or hidden randomness. Do not enable caching for tasks that depend on them. Concurrency locking, crash recovery and broader environment capture are still open M2 work. Reuse is disabled by default.

## Limits

- A task can still read other files, use the network or spawn child processes; the temporary directory is organizational isolation, not a security boundary. A timeout kills the direct process, not necessarily its descendants.
- Experimental M2 caching is opt-in; concurrent runs are not locked, dependency versions are not pinned, and arbitrary commands are not guaranteed deterministic.
- Source bytes are copied into the object store before a task runs. This records the bytes used, but does not establish authenticity of external data or prevent a malicious command from affecting the host.
- Verification checks internal consistency and stored bytes, not a signed attestation. A party able to modify the ledger and all objects can forge a consistent history.
- Large files are copied; streaming/zero-copy optimizations are planned only after measurement.

This repository is newly created for portfolio work and cannot qualify as a pre-existing repository under the linked Feishu collection rules.
