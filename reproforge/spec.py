"""Strict, deterministic validation of versioned pipeline specifications."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


class SpecError(Exception):
    """Invalid project specification or unsafe path."""


ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]*\Z")


def relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise SpecError(f"{label} must be a nonempty relative POSIX path")
    parts = value.split("/")
    if (any(part in ("", ".", "..") or ":" in part or part[-1] in (" ", ".")
            or any(ord(char) < 32 for char in part) or PureWindowsPath(part).is_reserved() for part in parts)
            or parts[0] == ".reproforge"):
        raise SpecError(f"{label} contains a forbidden path segment")
    return value


@dataclass(frozen=True)
class Input:
    destination: str
    project: str | None = None
    task: str | None = None
    artifact: str | None = None


@dataclass(frozen=True)
class Task:
    id: str
    command: tuple[str, ...]
    inputs: tuple[Input, ...]
    outputs: tuple[str, ...]
    depends_on: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class Plan:
    path: Path
    root: Path
    tasks: tuple[Task, ...]
    raw_bytes: bytes


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise SpecError(f"{label} must be a list")
    return value


def _overlap(paths: tuple[str, ...] | set[str]) -> bool:
    names = set(paths)
    return any("/".join(path.split("/")[:index]) in names
               for path in names for index in range(1, len(path.split("/"))))


def _task(value: Any, number: int) -> Task:
    label = f"tasks[{number}]"
    if not isinstance(value, dict) or set(value) - {"id", "command", "inputs", "outputs", "depends_on", "timeout_seconds"}:
        raise SpecError(f"{label} is not a task object or has unknown fields")
    task_id = value.get("id")
    if not isinstance(task_id, str) or not ID_PATTERN.fullmatch(task_id):
        raise SpecError(f"{label}.id must match [a-z][a-z0-9_-]*")
    command = _list(value.get("command"), f"{task_id}.command")
    if not command or any(not isinstance(word, str) or not word for word in command):
        raise SpecError(f"{task_id}.command must contain nonempty argument strings")
    raw_inputs = _list(value.get("inputs", []), f"{task_id}.inputs")
    inputs = []
    destinations = set()
    for index, item in enumerate(raw_inputs):
        item_label = f"{task_id}.inputs[{index}]"
        if not isinstance(item, dict):
            raise SpecError(f"{item_label} must be an object")
        destination = relative_path(item.get("as"), f"{item_label}.as")
        if destination in destinations:
            raise SpecError(f"{task_id}: duplicate input destination {destination}")
        destinations.add(destination)
        if set(item) == {"project", "as"}:
            inputs.append(Input(destination, project=relative_path(item["project"], f"{item_label}.project")))
        elif set(item) == {"task", "artifact", "as"}:
            source_task = item["task"]
            if not isinstance(source_task, str) or not ID_PATTERN.fullmatch(source_task):
                raise SpecError(f"{item_label}.task is invalid")
            inputs.append(Input(destination, task=source_task,
                                artifact=relative_path(item["artifact"], f"{item_label}.artifact")))
        else:
            raise SpecError(f"{item_label} must name either project+as or task+artifact+as")
    if _overlap(destinations):
        raise SpecError(f"{task_id}: input destinations overlap")
    outputs = tuple(relative_path(item, f"{task_id}.outputs") for item in _list(value.get("outputs", []), f"{task_id}.outputs"))
    if len(set(outputs)) != len(outputs):
        raise SpecError(f"{task_id}: duplicate outputs")
    if _overlap(outputs):
        raise SpecError(f"{task_id}: output paths overlap")
    for output in outputs:
        if output in destinations or any(output.startswith(item + "/") or item.startswith(output + "/") for item in destinations):
            raise SpecError(f"{task_id}: output overlaps an input path: {output}")
    raw_deps = _list(value.get("depends_on", []), f"{task_id}.depends_on")
    if any(not isinstance(item, str) or not ID_PATTERN.fullmatch(item) for item in raw_deps) or len(set(raw_deps)) != len(raw_deps):
        raise SpecError(f"{task_id}.depends_on must contain unique task IDs")
    timeout = value.get("timeout_seconds", 300)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise SpecError(f"{task_id}.timeout_seconds must be an integer from 1 to 3600")
    return Task(task_id, tuple(command), tuple(inputs), outputs, tuple(raw_deps), timeout)


def _ordered(tasks: list[Task]) -> tuple[Task, ...]:
    by_id = {task.id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise SpecError("Duplicate task IDs")
    dependencies = {}
    for task in tasks:
        refs = set(task.depends_on)
        refs.update(item.task for item in task.inputs if item.task is not None)
        if task.id in refs:
            raise SpecError(f"{task.id}: cannot depend on itself")
        unknown = refs - set(by_id)
        if unknown:
            raise SpecError(f"{task.id}: unknown dependencies: {', '.join(sorted(unknown))}")
        for item in task.inputs:
            if item.task is not None and item.artifact not in by_id[item.task].outputs:
                raise SpecError(f"{task.id}: {item.task} does not declare output {item.artifact}")
        dependencies[task.id] = refs
    state: dict[str, int] = {}
    result = []

    def visit(task_id: str) -> None:
        if state.get(task_id) == 1:
            raise SpecError(f"Dependency cycle includes {task_id}")
        if state.get(task_id) == 2:
            return
        state[task_id] = 1
        for predecessor in tasks:
            if predecessor.id in dependencies[task_id]:
                visit(predecessor.id)
        state[task_id] = 2
        result.append(by_id[task_id])

    for task in tasks:
        visit(task.id)
    return tuple(result)


def load_plan(path: Path) -> Plan:
    path = path.resolve()
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpecError(f"Cannot read project specification {path}: {exc}") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"version", "tasks"} or type(parsed["version"]) is not int or parsed["version"] != 1:
        raise SpecError("Project specification must contain version=1 and tasks only")
    raw_tasks = _list(parsed["tasks"], "tasks")
    if not raw_tasks:
        raise SpecError("Project needs at least one task")
    tasks = [_task(item, index) for index, item in enumerate(raw_tasks)]
    return Plan(path, path.parent, _ordered(tasks), raw)
