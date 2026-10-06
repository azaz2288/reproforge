"""Execute declared file DAGs and write verifiable run records."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .gates import evaluate
from .json_evidence import equal as equal_json, loads
from .locking import project_lock
from .spec import Plan, SpecError, Task
from .storage import Store, StorageError, atomic_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _within(root: Path, relative: str, label: str) -> Path:
    target = root.joinpath(*relative.split("/"))
    resolved_root = root.resolve()
    if not target.resolve(strict=False).is_relative_to(resolved_root):
        raise StorageError(f"{label} escapes its root: {relative}")
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise StorageError(f"{label} uses a symbolic link: {relative}")
    return target


def _log_record(store: Store, path: Path) -> dict[str, Any]:
    reference = store.put(path)
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        preview = stream.read(4096)
    return {**reference, "preview": preview, "truncated": reference["size"] > 4096}


def _cache_key(plan: Plan, task: Task, inputs: list[dict[str, Any]], environment: dict[str, str]) -> str:
    material = {"plan_sha256": hashlib.sha256(plan.raw_bytes).hexdigest(), "task_id": task.id,
                "command": task.command, "timeout_seconds": task.timeout_seconds,
                "outputs": task.outputs, "inputs": inputs, "environment": environment}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cache_hit(store: Store, key: str, task: Task, plan: Plan,
               inputs: list[dict[str, Any]], environment: dict[str, str]) -> dict[str, Any] | None:
    path = store.cache / f"{key}.json"
    if path.is_symlink():
        raise StorageError(f"Cache index is a symbolic link: {path}")
    if not path.exists():
        return None
    try:
        cached = loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise StorageError(f"Cannot read cache index {path}: {exc}") from exc
    if (not isinstance(cached, dict) or type(cached.get("version")) is not int or cached.get("version") != 1 or cached.get("key") != key
            or not isinstance(cached.get("source_run_id"), str)
            or not isinstance(cached.get("outputs"), dict) or set(cached["outputs"]) != set(task.outputs)
            or not isinstance(cached.get("stdout"), dict) or not isinstance(cached.get("stderr"), dict)):
        raise StorageError(f"Malformed cache index: {path}")
    source_id = cached["source_run_id"]
    if len(source_id) != 32 or any(char not in "0123456789abcdef" for char in source_id):
        raise StorageError(f"Malformed cache source run ID: {path}")
    source_path = store.runs / f"{source_id}.json"
    try:
        source_run = loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise StorageError(f"Cannot read cache source run {source_id}: {exc}") from exc
    if (not isinstance(source_run, dict) or type(source_run.get("version")) is not int or source_run.get("version") != 1
            or source_run.get("run_id") != source_id or source_run.get("status") != "success"
            or source_run.get("plan_sha256") != hashlib.sha256(plan.raw_bytes).hexdigest()
            or source_run.get("environment") != environment
            or not isinstance(source_run.get("tasks"), list)):
        raise StorageError(f"Malformed cache source run: {source_id}")
    matching = [item for item in source_run.get("tasks", []) if isinstance(item, dict) and item.get("id") == task.id]
    if (len(matching) != 1 or matching[0].get("status") != "success"
            or type(matching[0].get("returncode")) is not int or matching[0].get("returncode") != 0 or matching[0].get("command") != list(task.command)
            or matching[0].get("inputs") != inputs or "cached_from" in matching[0]
            or matching[0].get("cache_key") != key or any(matching[0].get(field) != cached[field]
            for field in ("outputs", "stdout", "stderr"))):
        raise StorageError(f"Cache index does not match source run: {path}")
    for reference in (*cached["outputs"].values(), cached["stdout"], cached["stderr"]):
        if not isinstance(reference, dict):
            raise StorageError(f"Malformed cache object reference: {path}")
        checked = {field: reference.get(field) for field in ("sha256", "size")}
        issues = store.check(checked)
        if issues:
            raise StorageError(f"Cache object failed validation: {issues[0]}")
    for stream in ("stdout", "stderr"):
        reference = cached[stream]
        with store.object_path(reference["sha256"]).open("r", encoding="utf-8", errors="replace") as source:
            preview = source.read(4096)
        if reference.get("preview") != preview or reference.get("truncated") is not (reference["size"] > 4096):
            raise StorageError(f"Cache {stream} preview metadata mismatch: {path}")
    return cached


def _record_cache(store: Store, key: str, run_id: str, entry: dict[str, Any]) -> None:
    value = {"version": 1, "key": key, "source_run_id": run_id, "outputs": entry["outputs"],
             "stdout": entry["stdout"], "stderr": entry["stderr"]}
    atomic_json(store.cache / f"{key}.json", value)


def _task_run(plan: Plan, task: Task, store: Store, completed: dict[str, dict[str, Any]],
              environment: dict[str, str], reuse: bool,
              resume_entry: dict[str, Any] | None = None, resume_id: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": task.id, "command": list(task.command), "status": "running",
                             "inputs": [], "outputs": {}, "started_at": _now()}
    store.temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{task.id}-", dir=store.temporary) as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        for input_spec in task.inputs:
            destination = _within(workspace, input_spec.destination, "Input destination")
            if input_spec.project is not None:
                source = _within(plan.root, input_spec.project, "Project input")
                if not source.is_file():
                    raise StorageError(f"Missing project input: {input_spec.project}")
                reference = store.put(source)
                origin = {"project": input_spec.project}
            else:
                assert input_spec.task is not None and input_spec.artifact is not None
                predecessor = completed.get(input_spec.task)
                if predecessor is None or input_spec.artifact not in predecessor["outputs"]:
                    raise StorageError(f"Missing dependency artifact: {input_spec.task}/{input_spec.artifact}")
                reference = predecessor["outputs"][input_spec.artifact]
                origin = {"task": input_spec.task, "artifact": input_spec.artifact}
            store.materialize(reference, destination)
            entry["inputs"].append({"as": input_spec.destination, "source": origin, "object": reference})

        cache_key = _cache_key(plan, task, entry["inputs"], environment) if task.cache else None
        if cache_key is not None:
            entry["cache_key"] = cache_key
        if resume_entry is not None:
            if resume_entry.get("inputs") != entry["inputs"]:
                raise StorageError(f"Cannot resume {task.id}: declared inputs changed")
            for reference in (*resume_entry["outputs"].values(), resume_entry["stdout"], resume_entry["stderr"]):
                object_ref = {field: reference.get(field) for field in ("sha256", "size")}
                issues = store.check(object_ref)
                if issues:
                    raise StorageError(f"Cannot resume {task.id}: {issues[0]}")
            entry.update({"status": "success", "returncode": 0, "outputs": resume_entry["outputs"],
                          "stdout": resume_entry["stdout"], "stderr": resume_entry["stderr"],
                          "resumed_from": resume_id, "finished_at": _now()})
            return entry
        if reuse and cache_key is not None:
            cached = _cache_hit(store, cache_key, task, plan, entry["inputs"], environment)
            if cached is not None:
                entry.update({"status": "success", "returncode": 0, "outputs": cached["outputs"],
                              "stdout": cached["stdout"], "stderr": cached["stderr"],
                              "cached_from": cached["source_run_id"], "finished_at": _now()})
                return entry

        stdout_path = root / "stdout.log"
        stderr_path = root / "stderr.log"
        if task.gate is not None:
            report = evaluate(task.gate, workspace)
            report_path = workspace / "gate-report.json"
            atomic_json(report_path, report)
            stdout_path.write_bytes(b"")
            stderr_path.write_bytes(b"")
            entry["stdout"] = _log_record(store, stdout_path)
            entry["stderr"] = _log_record(store, stderr_path)
            entry["outputs"]["gate-report.json"] = store.put(report_path)
            entry["status"] = "success" if report["passed"] else "failed"
            entry["returncode"] = 0 if report["passed"] else 1
            if not report["passed"]:
                entry["error"] = "; ".join(report["violations"])
            entry["finished_at"] = _now()
            return entry
        command = [sys.executable if index == 0 and word == "@python" else word
                   for index, word in enumerate(task.command)]
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = subprocess.Popen(command, cwd=workspace, stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, shell=False)
                try:
                    returncode = process.wait(timeout=task.timeout_seconds)
                    entry["returncode"] = returncode
                    entry["status"] = "success" if returncode == 0 else "failed"
                    if returncode != 0:
                        entry["error"] = f"Command exited with status {returncode}"
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    entry["returncode"] = None
                    entry["status"] = "timeout"
                    entry["error"] = f"Command exceeded {task.timeout_seconds} seconds"
            except OSError as exc:
                entry["returncode"] = None
                entry["status"] = "failed"
                entry["error"] = f"Cannot launch command: {exc}"
        entry["stdout"] = _log_record(store, stdout_path)
        entry["stderr"] = _log_record(store, stderr_path)
        if entry["status"] == "success":
            for relative in task.outputs:
                output = _within(workspace, relative, "Task output")
                if not output.is_file():
                    entry["status"] = "failed"
                    entry["error"] = f"Declared output missing or not a file: {relative}"
                    break
                entry["outputs"][relative] = store.put(output)
    entry["finished_at"] = _now()
    return entry


def execute(plan: Plan, reuse: bool = False, resume: str | None = None) -> dict[str, Any]:
    store = Store(plan.root / ".reproforge")
    with project_lock(store.root):
        return _execute_locked(plan, store, reuse, resume)


def _execute_locked(plan: Plan, store: Store, reuse: bool, resume: str | None) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    environment = {"python": sys.version.split()[0], "platform": platform.platform(),
                   "executable": str(Path(sys.executable).resolve())}
    resume_entries: list[dict[str, Any]] = []
    if resume is not None:
        problems = verify(plan, resume)
        if problems:
            raise StorageError(f"Cannot resume run {resume}: {problems[0]}")
        try:
            source = loads((store.runs / f"{resume}.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise StorageError(f"Cannot read resume source run {resume}: {exc}") from exc
        if source.get("status") not in ("failed", "interrupted") or source.get("environment") != environment:
            raise StorageError("Resume source must be failed/interrupted with the same runtime environment")
        for candidate in source["tasks"]:
            if candidate.get("status") != "success":
                break
            resume_entries.append(candidate)
    record: dict[str, Any] = {"version": 1, "run_id": run_id, "status": "running",
                              "plan_sha256": hashlib.sha256(plan.raw_bytes).hexdigest(),
                              "created_at": _now(), "environment": environment, "tasks": []}
    path = store.runs / f"{run_id}.json"
    atomic_json(path, record)
    completed: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(plan.tasks):
        try:
            entry = _task_run(plan, task, store, completed, environment, reuse,
                              resume_entries[index] if index < len(resume_entries) else None, resume)
        except (OSError, StorageError, SpecError) as exc:
            entry = {"id": task.id, "command": list(task.command), "status": "failed", "inputs": [],
                     "outputs": {}, "error": str(exc), "started_at": _now(), "finished_at": _now()}
        record["tasks"].append(entry)
        if entry["status"] == "success":
            completed[task.id] = entry
        else:
            record["status"] = "failed"
            record["finished_at"] = _now()
            atomic_json(path, record)
            return record
        atomic_json(path, record)
    record["status"] = "success"
    record["finished_at"] = _now()
    atomic_json(path, record)
    for task, entry in zip(plan.tasks, record["tasks"]):
        if task.cache and "cached_from" not in entry and "resumed_from" not in entry and "cache_key" in entry:
            try:
                _record_cache(store, entry["cache_key"], run_id, entry)
            except StorageError as exc:
                # Cache is optional; a completed task and its ledger remain valid.
                entry["cache_warning"] = str(exc)
                atomic_json(path, record)
    return record


def recover(plan: Plan) -> list[str]:
    """Mark abandoned running ledgers interrupted while holding the writer lock."""
    store = Store(plan.root / ".reproforge")
    changed = []
    with project_lock(store.root):
        if not store.runs.is_dir():
            return changed
        for path in store.runs.glob("*.json"):
            if path.is_symlink() or len(path.stem) != 32 or any(char not in "0123456789abcdef" for char in path.stem):
                continue
            try:
                record = loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            if (not isinstance(record, dict) or type(record.get("version")) is not int
                    or record.get("version") != 1 or record.get("run_id") != path.stem
                    or record.get("plan_sha256") != hashlib.sha256(plan.raw_bytes).hexdigest()
                    or record.get("status") != "running"):
                continue
            record["status"] = "interrupted"
            record["finished_at"] = _now()
            record["recovery_note"] = "A prior process stopped before the run completed; no task was resumed."
            atomic_json(path, record)
            changed.append(path.stem)
    return changed


def verify(plan: Plan, run_id: str, _seen: frozenset[str] = frozenset()) -> list[str]:
    if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
        raise StorageError("Invalid run ID")
    if run_id in _seen:
        return [f"Resume source cycle includes {run_id}"]
    if len(_seen) >= 64:
        return ["Resume source chain exceeds 64 runs"]
    _seen = _seen | {run_id}
    store = Store(plan.root / ".reproforge")
    path = store.runs / f"{run_id}.json"
    if path.is_symlink():
        raise StorageError(f"Run ledger is a symbolic link: {run_id}")
    try:
        record = loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise StorageError(f"Cannot read run {run_id}: {exc}") from exc
    if not isinstance(record, dict) or type(record.get("version")) is not int or record.get("version") != 1 or record.get("run_id") != run_id or not isinstance(record.get("tasks"), list):
        raise StorageError("Malformed or unsupported run record")
    issues = []
    if record.get("plan_sha256") != hashlib.sha256(plan.raw_bytes).hexdigest():
        issues.append("Current project specification differs from the recorded run")
    if record.get("status") not in ("success", "failed", "running", "interrupted"):
        issues.append("Invalid run status")
    previous: dict[str, dict[str, Any]] = {}
    environment = record.get("environment")
    if not isinstance(environment, dict):
        raise StorageError("Malformed recorded environment")
    for index, entry in enumerate(record["tasks"]):
        if not isinstance(entry, dict) or index >= len(plan.tasks):
            raise StorageError("Malformed or unexpected task entry")
        task = plan.tasks[index]
        if entry.get("id") != task.id or entry.get("command") != list(task.command):
            issues.append(f"Task {index + 1} does not match the plan")
        if entry.get("status") not in ("success", "failed", "timeout"):
            issues.append(f"{task.id}: invalid task status")
        if "returncode" in entry and entry['returncode'] is not None and type(entry['returncode']) is not int:
            issues.append(f"{task.id}: exit code must be an integer or null")
        if entry.get("status") == "success" and entry.get("returncode") != 0:
            issues.append(f"{task.id}: successful task has a nonzero or missing exit code")
        if entry.get("status") == "timeout" and entry.get("returncode") is not None:
            issues.append(f"{task.id}: timed-out task has an exit code")
        outputs = entry.get("outputs")
        inputs = entry.get("inputs")
        if not isinstance(outputs, dict) or not isinstance(inputs, list):
            raise StorageError(f"Malformed references in task {task.id}")
        if entry.get("status") == "success" and set(outputs) != set(task.outputs):
            issues.append(f"{task.id}: declared outputs differ from run record")
        if task.gate is not None and "returncode" in entry and set(outputs) != {"gate-report.json"}:
            issues.append(f"{task.id}: gate report is missing")
        if task.gate is not None and "returncode" in entry and entry.get("returncode") != (0 if entry.get("status") == "success" else 1):
            issues.append(f"{task.id}: gate exit status mismatch")
        if entry.get("status") == "success" and len(inputs) != len(task.inputs):
            issues.append(f"{task.id}: input count differs from plan")
        if task.cache and len(inputs) == len(task.inputs):
            expected_key = _cache_key(plan, task, inputs, environment)
            if entry.get("cache_key") != expected_key:
                issues.append(f"{task.id}: cache key mismatch")
        elif entry.get("cache_key") is not None or entry.get("cached_from") is not None:
            issues.append(f"{task.id}: cache metadata on a non-cacheable or incomplete task")
        for input_index, item in enumerate(inputs):
            if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
                raise StorageError(f"Malformed input in task {task.id}")
            if input_index < len(task.inputs):
                spec = task.inputs[input_index]
                expected_source = {"project": spec.project} if spec.project is not None else {"task": spec.task, "artifact": spec.artifact}
                if item.get("as") != spec.destination or item["source"] != expected_source:
                    issues.append(f"{task.id}: input contract mismatch")
                if spec.task is not None:
                    upstream = previous.get(spec.task, {}).get("outputs", {}).get(spec.artifact)
                    if upstream != item.get("object"):
                        issues.append(f"{task.id}: dependency artifact reference mismatch")
            issues.extend(store.check(item.get("object")))
        for output_name, reference in outputs.items():
            if output_name not in task.outputs:
                issues.append(f"{task.id}: undeclared output {output_name}")
            issues.extend(store.check(reference))
        if task.gate is not None and len(inputs) == 1 and "gate-report.json" in outputs:
            report_ref = outputs["gate-report.json"]
            if not store.check(report_ref) and not store.check(inputs[0].get("object")):
                with tempfile.TemporaryDirectory(prefix="verify-gate-") as temporary:
                    gate_workspace = Path(temporary)
                    gate_input = _within(gate_workspace, task.gate["input"], "Gate input")
                    store.materialize(inputs[0]["object"], gate_input)
                    expected = evaluate(task.gate, gate_workspace)
                try:
                    actual = loads(store.object_path(report_ref["sha256"]).read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError) as exc:
                    raise StorageError(f"Cannot read gate report for {task.id}: {exc}") from exc
                if not equal_json(actual, expected):
                    issues.append(f"{task.id}: gate report differs from independently evaluated input")
                if (entry.get("status") == "success") != expected["passed"]:
                    issues.append(f"{task.id}: gate status differs from report")
        for stream in ("stdout", "stderr"):
            if stream in entry:
                reference = entry[stream]
                if not isinstance(reference, dict):
                    raise StorageError(f"Malformed {stream} reference in task {task.id}")
                object_reference = {key: reference.get(key) for key in ("sha256", "size")}
                stream_issues = store.check(object_reference)
                issues.extend(stream_issues)
                if not stream_issues:
                    with store.object_path(reference["sha256"]).open("r", encoding="utf-8", errors="replace") as source:
                        actual_preview = source.read(4096)
                    if reference.get("preview") != actual_preview or reference.get("truncated") is not (reference["size"] > 4096):
                        issues.append(f"{task.id}: {stream} preview metadata mismatch")
            elif entry.get("status") in ("success", "timeout") or "returncode" in entry:
                issues.append(f"{task.id}: missing {stream} log reference")
        if "cached_from" in entry:
            source_id = entry["cached_from"]
            if (not isinstance(source_id, str) or len(source_id) != 32
                    or any(char not in "0123456789abcdef" for char in source_id)):
                issues.append(f"{task.id}: invalid cache source run ID")
            else:
                source_path = store.runs / f"{source_id}.json"
                try:
                    source_record = loads(source_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError) as exc:
                    raise StorageError(f"Cannot read cache source run {source_id}: {exc}") from exc
                if not isinstance(source_record, dict) or not isinstance(source_record.get("tasks"), list):
                    issues.append(f"{task.id}: malformed cache source run")
                    continue
                source_entries = source_record["tasks"]
                candidates = [item for item in source_entries if isinstance(item, dict) and item.get("id") == task.id]
                if (type(source_record.get("version")) is not int or source_record.get("version") != 1 or source_record.get("run_id") != source_id
                        or source_record.get("status") != "success"
                        or source_record.get("plan_sha256") != record.get("plan_sha256")
                        or source_record.get("environment") != environment
                        or len(candidates) != 1 or candidates[0].get("status") != "success"
                        or type(candidates[0].get("returncode")) is not int or candidates[0].get("returncode") != 0
                        or candidates[0].get("inputs") != inputs
                        or "cached_from" in candidates[0]
                        or candidates[0].get("cache_key") != entry.get("cache_key")
                        or any(candidates[0].get(field) != entry.get(field) for field in ("outputs", "stdout", "stderr"))):
                    issues.append(f"{task.id}: cached artifacts differ from source run")
        if "resumed_from" in entry:
            source_id = entry["resumed_from"]
            if (not isinstance(source_id, str) or len(source_id) != 32
                    or any(char not in "0123456789abcdef" for char in source_id)):
                issues.append(f"{task.id}: invalid resume source run ID")
            else:
                try:
                    source_record = loads((store.runs / f"{source_id}.json").read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError) as exc:
                    raise StorageError(f"Cannot read resume source run {source_id}: {exc}") from exc
                source_tasks = source_record.get("tasks") if isinstance(source_record, dict) else None
                source_entry = source_tasks[index] if isinstance(source_tasks, list) and index < len(source_tasks) else None
                if (not isinstance(source_record, dict) or source_id == run_id
                        or source_record.get("run_id") != source_id
                        or source_record.get("status") not in ("failed", "interrupted")
                        or source_record.get("plan_sha256") != record.get("plan_sha256")
                        or source_record.get("environment") != environment
                        or not isinstance(source_entry, dict) or source_entry.get("id") != task.id
                        or source_entry.get("status") != "success"
                        or type(source_entry.get("returncode")) is not int or source_entry.get("returncode") != 0
                        or source_entry.get("command") != list(task.command)
                        or any(source_entry.get(field) != entry.get(field)
                               for field in ("inputs", "outputs", "stdout", "stderr"))):
                    issues.append(f"{task.id}: resumed artifacts differ from source run")
                else:
                    for source_issue in verify(plan, source_id, _seen):
                        issues.append(f"{task.id}: resume source: {source_issue}")
        previous[task.id] = entry
    if record.get("status") == "success" and len(record["tasks"]) != len(plan.tasks):
        issues.append("Successful run does not contain every task")
    if record.get("status") == "success" and any(entry.get("status") != "success" for entry in record["tasks"]):
        issues.append("Successful run contains a failed or timed-out task")
    if record.get("status") == "failed" and record["tasks"] and record["tasks"][-1].get("status") == "success":
        issues.append("Failed run ends with a successful task")
    if record.get("status") == "failed" and not record["tasks"]:
        issues.append("Failed run has no task record")
    if record.get("status") == "running" and "finished_at" in record:
        issues.append("Running record has a finish time")
    if record.get("status") == "running":
        issues.append("Run is incomplete and cannot be verified as finished")
    if record.get("status") == "interrupted" and "finished_at" not in record:
        issues.append("Interrupted run is missing its recovery time")
    return issues
